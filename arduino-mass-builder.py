#!/usr/bin/env python3

# Copyright (c) 2014 Matthijs Kooijman <matthijs@stdin.nl>
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import os
import re
import sys
import json
import click
import fnmatch
import pathlib
import shutil
import subprocess
import hashlib
from itertools import takewhile
from functools import wraps

import csv

arduino_cmd = 'arduino-git'
size_command = 'avr-size'
report_attrs = ['buildset', 'sketch_dir', 'board', 'status', 'program_size', 'data_size']
delta_attrs = ['delta_status', 'delta_program_size', 'delta_data_size', 'is_base']
report_headers = {
    'buildset': 'Buildset',
    'sketch_dir': 'Sketch',
    'board': 'Board',
    'status': 'Status',
    'program_size': 'Program size',
    'data_size': 'Data size',
    'delta_status': 'Δ status',
    'delta_program_size': 'Δ program size',
    'delta_data_size': 'Δ data size',
    'is_base': 'In base buildset',
}

class Path(click.Path):
    """
    Variant on click.path that returns a pathlib.Path object instead of
    a string.
    """
    def convert(self, value, param, ctx):
        return pathlib.Path(super().convert(value, param, ctx))

def explodepath(path):
    parts = []
    while True:
        head, tail = os.path.split(path)
        if head == path and not tail:
            parts.append(head)
            break
        parts.append(tail)
        path = head

    parts.reverse()
    return parts


def commonpath(paths):
    exploded = [os.path.normpath(p).split(os.path.sep) for p in paths]
    zipped = list(zip(*exploded))
    def is_prefix(level):
        return len(level) == len(zipped[0]) and all(n==level[0] for n in level[1:])

    return os.path.join(*[x[0] for x in takewhile(is_prefix, zipped)])

class Options:
    def __init__(self, **options):
        self.__dict__.update(**options)

def pass_opts(f):
    @wraps(f)
    @click.pass_context
    def wrapper(ctx, *args, **kwargs):
        opts = Options(**ctx.params)
        while ctx.parent:
            ctx = ctx.parent
        opts.main = Options(**ctx.params)

        return ctx.invoke(f, *args, opts=opts, **kwargs)
    return wrapper

@click.group()
@click.option('--verbose', '-v', help='More verbose output', count=True)
def main(**kwargs):
    pass

@main.command()
@pass_opts
@click.pass_context
@click.option('--results-dir', '-r', default='result', type=Path(), help='Directory to store results in')
@click.option('--boards', '-b', default='arduino:avr:uno', help='Boards to build for (can contain whitespace or comma-separated values)')
@click.option('--buildset', '-s', default='base', help='Arbitrary name to identify these builds')
@click.option('--force/--no-force', '-f', default=False, help='Overwrite existing builds')
@click.argument('sketches', nargs=-1, type=Path(exists=True, dir_okay=False, readable=True))
def build(ctx, opts, sketches, boards, **kwargs):
    for sketch in sketches:
        if sketch.is_absolute() or sketch.parts[0] == '..':
            ctx.fail("Sketch filenames must be relative paths, inside the current directory")

    boardlist = re.split('[\s,]+', boards)
    for sketch in sketches:
        for board in boardlist:
            do_compile(opts, sketch, board)

@main.command()
@pass_opts
@click.pass_context
@click.option('--results-dir', '-r', default='result', type=Path(exists=True, file_okay=False), help='Directory to read results from')
@click.option('--base-set', '-B', help='Base buildset to compare things against')
@click.argument('sketches', nargs=-1, type=Path(exists=True, dir_okay=False, readable=True))
def report(ctx, opts, sketches, **kwargs):
    report_dir = opts.results_dir / 'report'
    if not report_dir.exists():
        report_dir.mkdir(parents=True)
    (data, buildsets) = create_report_data(opts.results_dir)
    if len(buildsets) > 1 and not opts.base_set and 'base' in buildsets:
        opts.base_set = 'base'

    if opts.base_set:
        add_delta_info(data, opts.base_set)

    #with (report_dir / 'data.json').open('w') as f:
    #    json.dump(list(data), f)

    with (report_dir / 'data.csv').open('w') as f:
        csv.writer(f).writerow(build_report_row(report_headers, opts.base_set))
        for (key, build) in sorted(data.items()):
            csv.writer(f).writerow(build_report_row(build, opts.base_set))

def build_report_row(build, delta):
    row = [build.get(attr, '') for attr in report_attrs]
    if delta:
        row += [build.get(attr, '') for attr in delta_attrs]
    return row

def add_extra_info(path, build):
    """
    Add extra info (sizes, build result checksum) to the given record.
    This looks at the actual compiled file to find out the info.
    """
    elffile = path / 'build' / (build['sketch_name'] + '.cpp.elf')
    hexfile = path / 'build' / (build['sketch_name'] + '.cpp.hex')
    for line in subprocess.check_output([size_command, '-C', str(elffile)]).splitlines():
        match = re.match(b'^Program: *([0-9]*) bytes$', line)
        if match:
            build['program_size'] = int(match.group(1))
        match = re.match(b'^Data: *([0-9]*) bytes$', line)
        if match:
            build['data_size'] = int(match.group(1))

    with open(str(hexfile), 'rb') as f:
        h = hashlib.sha1()
        h.update(f.read())
        build['hash'] = h.hexdigest()

def add_delta_info(data, base):
    for (key, build) in data.items():
        if build['buildset'] == base:
            build['is_base'] = 'Yes'
        else:
            build['is_base'] = 'No'


        if build['buildset'] == base:
            build['delta_status'] = 'Is base'
            if build['status'] == 'OK':
                build['delta_program_size'] = 0
                build['delta_data_size'] = 0
        else:
            try:
                base_build = data[(base, build['sketch_dir'], build['board'])]
            except KeyError:
                sys.stderr.write("{} / {} / {}: No corresponding build in base buildset found, cannot compare\n".format(build['buildset'], build['sketch_dir'], build['board']))
                build['delta_status'] = 'No base'
                continue

            if build['status'] == 'OK' and base_build['status'] == 'OK':
                if build['hash'] == base_build['hash']:
                    build['delta_status'] = 'Identical'
                else:
                    build['delta_status'] = 'Modified'
            else:
                if build['status'] == 'OK':
                    build['delta_status'] = 'Fixed'
                elif base_build['status'] == 'OK':
                    build['delta_status'] = 'Broken'
                else:
                    build['delta_status'] = 'Still broken'

            if build['status'] == 'OK' and base_build['status'] == 'OK':
                build['delta_program_size'] = build['program_size'] - base_build['program_size']
                build['delta_data_size'] = build['data_size'] - base_build['data_size']



def run_command(opts, cmd, output_file):
    with output_file.open('w') as out:
        if opts.main.verbose >= 1:
            sys.stdout.write("Running: {}\n".format(' '.join(cmd)))
        res = subprocess.call(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        if opts.main.verbose >= 1:
            sys.stdout.write("Returned {}\n".format(res))
    return res

def find_builds(result_dir):
    for (dirname, subdirs, files) in os.walk(str(result_dir)):
        dirname = pathlib.Path(dirname)
        if 'build.json' in files:
            # Don't traverse any subdirectories
            subdirs[:] = []
            # yield the pathname, relative to result_dir
            with (dirname / 'build.json').open('r') as f:
                yield (pathlib.Path(dirname), json.load(f))

def create_report_data(results_dir):
    result_dir = results_dir
    data = {}
    buildsets = set()
    for (path, build) in find_builds(result_dir):
        if build['exit_code'] != 0:
            build['status'] = 'Failed to compile'

        if not 'status' in build:
            try:
                add_extra_info(path, build)
            except subprocess.CalledProcessError:
                build['status'] = 'Failed to get size'

        if not 'status' in build:
            build['status'] = 'OK'

        data[(build['buildset'], build['sketch_dir'], build['board'])] = build
        buildsets.add(build['buildset'])
    return (data, buildsets)

def do_compile(opts, sketch, board):
    sketch_result_dir = opts.results_dir / opts.buildset / sketch.parent / board
    build_dir = sketch_result_dir / 'build'
    json_file = sketch_result_dir / 'build.json'
    if sketch_result_dir.exists():
        if not json_file.exists():
            if opts.main.verbose >= 1:
                sys.stdout.write("{} looks interrupted, removing\n".format(str(sketch.parent / board)))
            shutil.rmtree(str(sketch_result_dir))
        elif opts.force:
            if opts.main.verbose >= 1:
                sys.stdout.write("{} already exists, removing\n".format(str(sketch.parent / board)))
            shutil.rmtree(str(sketch_result_dir))
        else:
            if opts.main.verbose >= 1:
                sys.stdout.write("{} already exists, skipping\n".format(str(sketch.parent / board)))
            return

    build_dir.mkdir(parents=True)

    cmd = [arduino_cmd];
    cmd += ['--pref', 'build.path=' + str(build_dir.resolve())]
    cmd += ['--board', board]
    cmd += ['--verify', str(sketch.resolve())]
    cmd += ['--verbose']

    res = run_command(opts, cmd, sketch_result_dir / 'build.log')

    build = {
        'exit_code'     : res,
        'sketch_dir'    : str(sketch.parent),
        'sketch_name'   : sketch.stem,
        'board'         : board,
        'buildset'      : opts.buildset,
    }
    with json_file.open('w') as f:
        json.dump(build, f)

if __name__ == '__main__':
    sys.exit(main())
