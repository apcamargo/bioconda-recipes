#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

from collections import defaultdict
import copy
import json
import os
from os.path import join, isdir
import sys
import tqdm
import re
import requests
import pkg_resources

CHANNEL_NAME = "bioconda"
CHANNEL_ALIAS = "https://conda.anaconda.org"
SUBDIRS = (
    "noarch",
    "linux-64",
    "osx-64",
)

REMOVALS = {
    "noarch": (
    ),
    "linux-64": (
    ),
    "osx-64": (
    ),
}

OPERATORS = ["==", ">=", "<=", ">", "<", "!="]


def _add_removals(instructions, subdir):
    r = requests.get(
        "https://conda.anaconda.org/bioconda/"
        "label/broken/%s/repodata.json" % subdir
    )

    if r.status_code != 200:
        r.raise_for_status()

    data = r.json()
    currvals = list(REMOVALS.get(subdir, []))
    for pkg_name in data["packages"]:
        currvals.append(pkg_name)

    instructions["remove"].extend(tuple(set(currvals)))


def _gen_patch_instructions(index, new_index, subdir):
    instructions = {
        "patch_instructions_version": 1,
        "packages": defaultdict(dict),
        "revoke": [],
        "remove": [],
    }

    #_add_removals(instructions, subdir)

    # diff all items in the index and put any differences in the instructions
    for fn in index:
        assert fn in new_index

        # replace any old keys
        for key in index[fn]:
            assert key in new_index[fn], (key, index[fn], new_index[fn])
            if index[fn][key] != new_index[fn][key]:
                instructions['packages'][fn][key] = new_index[fn][key]

        # add any new keys
        for key in new_index[fn]:
            if key not in index[fn]:
                instructions['packages'][fn][key] = new_index[fn][key]

    return instructions


def has_dep(record, name):
    return any(dep.split(' ')[0] == name for dep in record.get('depends', ()))


def parse_version(version):
    if not isinstance(version, str):
        version = '.'.join(version)
    return pkg_resources.parse_version(version)


changes = set([])


def _gen_new_index(repodata, subdir):
    """Make any changes to the index by adjusting the values directly.

    This function returns the new index with the adjustments.
    Finally, the new and old indices are then diff'ed to produce the repo
    data patches.
    """
    index = copy.deepcopy(repodata["packages"])

    for fn, record in index.items():
        record_name = record["name"]
        version = record['version']
        deps = record.get("depends", ())

        # TBB 2021 (oneTBB 2021) is incompatible with previous releases.
        if has_dep(record, "tbb") and record.get('timestamp', 0) < 1614809400000:
            for i, dep in enumerate(deps):
                if dep == "tbb":
                    deps[i] = "tbb <2021.0.0a0"
                    break
                elif any(dep.startswith(f"tbb >={i}") for i in range(2017, 2021)) or dep.startswith("tbb >=4.4"):
                    deps[i] = "{},<2021.0.0a0".format(dep)
                    #deps.append("tbb <2021.0.0a0")
                    break

        # All R packages require a maximum version, so >=A.B,<A.C rather than >=A.B.D
        if (record_name.startswith('bioconductor-') or record_name.startswith('r-')) and has_dep(record, "r-base"):
            for i, dep in enumerate(deps):
                if dep.startswith('r-base >=') and '<' not in dep:
                    minVersion = dep.split('=')[1]
                    _ = minVersion.split('.')
                    if len(_) >= 2:
                        minor = str(int(_[1]) + 1)
                    minVersion = '.'.join([_[0], _[1]])
                    maxVersion = '.'.join([_[0], minor])
                    deps[i] = 'r-base >={},<{}'.format(minVersion, maxVersion)
                    break

        # Bioconductor data packages are noarch: generic and incorrectly pin curl to >=7.38.1,<8, rather than >=7,<8
        if subdir == "noarch" and record_name.startswith('bioconductor-') and has_dep(record, "curl"):
            for i, dep in enumerate(deps):
                if dep.startswith('curl >=7.'):
                    deps[i] = 'curl'
                    break

        # Old perl- packages don't pin perl-5.22, time cut-off is Jan 1, 2018
        if record_name.startswith('perl-') and (not has_dep(record, "perl")) and record.get('timestamp', 0) < 1514761200000:
            deps.append('perl >=5.22.0,<5.23.0')

        # Nanoqc requires bokeh >=2.4,<3
        if record_name.startswith('nanoqc') and has_dep(record, "bokeh") and record.get('timestamp', 0) < 1592397000000:
            for i, dep in enumerate(deps):
                if dep.startswith('bokeh'):
                    deps[i] = 'bokeh >=2.4,<3'
                    break

        # Pin all old packages that do not have a pin to openssl 1.1.1 which should have been available 
        # TODO once we have updated to openssl 3, below timestamp should be updated
        if has_dep(record, "openssl") and record.get("timestamp", 0) < 1678355208942:
            for i, dep in enumerate(deps):
                if dep.startswith("openssl") and has_no_upper_bound(dep):
                    deps[i] = "openssl >=1.1.0,<=1.1.1"
                    break

        # some htslib packages depend on openssl without this being listed in the dependencies
        if record_name.startswith('htslib') and record['subdir']=='linux-64' and not has_dep(record, "openssl") and record.get('timestamp', 0) < 1678355208942:
            for v, b in [("1.3", "1"), ("1.3.1", "0"), ("1.3.1", "1"), ("1.3.2", "0"), ("1.4", "0"), ("1.4.1", "0"), ("1.5", "0"), ("1.6", "0"), ("1.7", "0"), ("1.8", "0"), ("1.8", "1")]:
                if version==v and record['build']==b:
                    deps.append('openssl >=1.1.0,<=1.1.1')

        # add openssl dependency to old samtools packages that neither depend on htslib nor on openssl
        if record_name.startswith('samtools') and record['subdir']=='linux-64' and not has_dep(record, "openssl") and not has_dep(record, "htslib"):
            deps.append('openssl >=1.1.0,<=1.1.1')

        # future HTSlib versions are binary compatible until they bump their soversion
        if has_dep(record, 'htslib'):
            # skip deps prior to 1.10, which was the first with soversion 3
            # TODO adjust replacement (exclusive) upper bound with each new compatible HTSlib
            _pin_looser(fn, record, 'htslib', min_lower_bound='1.10', upper_bound='1.23')

        # future libdeflate versions are compatible until they bump their soversion; relax dependencies accordingly
        if record_name in ['htslib', 'staden_io_lib', 'fastp', 'pysam'] and has_dep(record, 'libdeflate'):
            # skip deps that allow anything <1.3, which contained an incompatible library filename
            # TODO adjust the replacement (exclusive) upper bound each time a compatible new libdeflate is released
            _pin_looser(fn, record, 'libdeflate', min_lower_bound='1.3', upper_bound='1.25')

        # nanosim <=3.1.0 requires scikit-learn<=0.22.1
        if record_name.startswith('nanosim') and has_dep(record, "scikit-learn") and version <= "3.1.0":
            for i, dep in enumerate(deps):
                if dep.startswith("scikit-learn") and has_no_upper_bound(dep):
                    deps[i] += ",<=0.22.1"  # append an upper bound
                    break

        # snakemake <8.1.2 requires pulp <2.8.0
        if record_name == 'snakemake-minimal' and has_dep(record, "pulp") and version < "8.1.2":
            for i, dep in enumerate(deps):
                if dep.startswith("pulp") and has_no_upper_bound(dep):
                    deps[i] = "pulp >=2.0,<2.8.0"


    return index


def has_no_upper_bound(dep):
    """Check if a dependency has an upper bound."""
    dep_parts = dep.split(' ', 1)
    bounds = dep_parts[1].split(",") if len(dep_parts) > 1 else []
    for bound in bounds:
        if bound.startswith("<"):
            # upper bound
            return False
        elif not (bound.startswith(">") or bound.startswith("<")):
            # If there is no < or > operator, then it is an exact pin (e.g. =1.2, or 1.2, or 1.2.*)
            return False
    return True


def _replace_pin(old_pin, new_pin, deps, record):
    """Replace an exact pin with a new one."""
    if old_pin in deps:
        i = record['depends'].index(old_pin)
        record['depends'][i] = new_pin


def _rename_dependency(fn, record, old_name, new_name):
    depends = record["depends"]
    dep_idx = next(
        (q for q, dep in enumerate(depends)
         if dep.split(' ')[0] == old_name),
        None
    )
    if dep_idx is not None:
        parts = depends[dep_idx].split(" ")
        remainder = (" " + " ".join(parts[1:])) if len(parts) > 1 else ""
        depends[dep_idx] = new_name + remainder
        record['depends'] = depends


def pad_list(l, num):
    if len(l) >= num:
        return l
    return l + ["0"]*(num - len(l))


def get_upper_bound(version, max_pin):
    num_x = max_pin.count("x")
    ver = pad_list(version.split("."), num_x)
    ver[num_x:] = ["0"]*(len(ver)-num_x)
    ver[num_x-1] = str(int(ver[num_x-1])+1)
    return ".".join(ver)


def _relax_exact(fn, record, fix_dep, max_pin=None):
    depends = record.get("depends", ())
    dep_idx = next(
        (q for q, dep in enumerate(depends)
         if dep.split(' ')[0] == fix_dep),
        None
    )
    if dep_idx is not None:
        dep_parts = depends[dep_idx].split(" ")
        if (len(dep_parts) == 3 and \
                not any(dep_parts[1].startswith(op) for op in OPERATORS)):
            if max_pin is not None:
                upper_bound = get_upper_bound(dep_parts[1], max_pin) + "a0"
                depends[dep_idx] = "{} >={},<{}".format(*dep_parts[:2], upper_bound)
            else:
                depends[dep_idx] = "{} >={}".format(*dep_parts[:2])
            record['depends'] = depends


cb_pin_regex = re.compile(r"^>=(?P<lower>\d(\.\d+)*a?),<(?P<upper>\d(\.\d+)*)a0$")

def _pin_stricter(fn, record, fix_dep, max_pin, upper_bound=None):
    depends = record.get("depends", ())
    dep_indices = [q for q, dep in enumerate(depends) if dep.split(' ')[0] == fix_dep]
    for dep_idx in dep_indices:
        dep_parts = depends[dep_idx].split(" ")
        if len(dep_parts) not in [2, 3]:
            continue
        m = cb_pin_regex.match(dep_parts[1])
        if m is None:
            continue
        lower = m.group("lower")
        upper = m.group("upper").split(".")
        if upper_bound is None:
            new_upper = get_upper_bound(lower, max_pin).split(".")
        else:
            new_upper = upper_bound.split(".")
        upper = pad_list(upper, len(new_upper))
        new_upper = pad_list(new_upper, len(upper))
        if parse_version(upper) > parse_version(new_upper):
            if str(new_upper[-1]) != "0":
                new_upper += ["0"]
            depends[dep_idx] = "{} >={},<{}a0".format(dep_parts[0], lower, ".".join(new_upper))
            if len(dep_parts) == 3:
                depends[dep_idx] = "{} {}".format(depends[dep_idx], dep_parts[2])
            record['depends'] = depends


def _pin_looser(fn, record, fix_dep, max_pin=None, upper_bound=None, min_lower_bound=None):
    depends = record.get("depends", ())
    dep_indices = [q for q, dep in enumerate(depends) if dep.split(' ')[0] == fix_dep]
    for dep_idx in dep_indices:
        dep_parts = depends[dep_idx].split(" ")
        if len(dep_parts) not in [2, 3]:
            continue
        m = cb_pin_regex.match(dep_parts[1])
        if m is None:
            continue
        lower = m.group("lower")
        upper = m.group("upper").split(".")

        if min_lower_bound is not None:
            if parse_version(lower) < parse_version(min_lower_bound):
                continue

        if upper_bound is None:
            new_upper = get_upper_bound(lower, max_pin).split(".")
        else:
            new_upper = upper_bound.split(".")

        upper = pad_list(upper, len(new_upper))
        new_upper = pad_list(new_upper, len(upper))

        if parse_version(upper) < parse_version(new_upper):
            if str(new_upper[-1]) != "0":
                new_upper += ["0"]
            depends[dep_idx] = "{} >={},<{}a0".format(dep_parts[0], lower, ".".join(new_upper))
            if len(dep_parts) == 3:
                depends[dep_idx] = "{} {}".format(depends[dep_idx], dep_parts[2])
            record['depends'] = depends



def _extract_feature(record, feature_name):
    features = record.get('features', '').split()
    features.remove(feature_name)
    return " ".join(features) or None


def _extract_track_feature(record, feature_name):
    features = record.get('track_features', '').split()
    features.remove(feature_name)
    return " ".join(features) or None


def main():
    # Step 1. Collect initial repodata for all subdirs.
    repodatas = {}
    for subdir in tqdm.tqdm(SUBDIRS, desc="Downloading repodata"):
        repodata_url = "/".join(
            (CHANNEL_ALIAS, CHANNEL_NAME, subdir, "repodata_from_packages.json"))
        response = requests.get(repodata_url)
        response.raise_for_status()
        repodatas[subdir] = response.json()

    # Step 2. Create all patch instructions.
    prefix_dir = os.getenv("PREFIX", "patches")
    for subdir in SUBDIRS:
        prefix_subdir = join(prefix_dir, subdir)
        if not isdir(prefix_subdir):
            os.makedirs(prefix_subdir)

        # Step 2a. Generate a new index.
        new_index = _gen_new_index(repodatas[subdir], subdir)

        # Step 2b. Generate the instructions by diff'ing the indices.
        instructions = _gen_patch_instructions(
            repodatas[subdir]['packages'], new_index, subdir)

        # Step 2c. Output this to $PREFIX so that we bundle the JSON files.
        patch_instructions_path = join(
            prefix_subdir, "patch_instructions.json")
        with open(patch_instructions_path, 'w') as fh:
            json.dump(
                instructions, fh, indent=2,
                sort_keys=True, separators=(',', ': '))


if __name__ == "__main__":
    sys.exit(main())
