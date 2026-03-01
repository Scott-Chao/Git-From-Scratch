import argparse
import configparser
from datetime import datetime
from fnmatch import fnmatch
import hashlib
from math import ceil
import os
import re
import sys
import zlib

try:
    import grp, pwd
except ModuleNotFoundError:
    pass

argparser = argparse.ArgumentParser(description="The stupidest content tracker")
argsubparsers = argparser.add_subparsers(title="Commands", dest="command")
argsubparsers.required = True


def main(argv=sys.argv[1:]):
    args = argparser.parse_args(argv)
    match args.command:
        case "add":
            cmd_add(args)
        case "cat-file":
            cmd_cat_file(args)
        case "check-ignore":
            cmd_check_ignore(args)
        case "checkout":
            cmd_checkout(args)
        case "commit":
            cmd_commit(args)
        case "hash-object":
            cmd_hash_object(args)
        case "init":
            cmd_init(args)
        case "log":
            cmd_log(args)
        case "ls-files":
            cmd_ls_files(args)
        case "ls-tree":
            cmd_ls_tree(args)
        case "rev-parse":
            cmd_rev_parse(args)
        case "rm":
            cmd_rm(args)
        case "show-ref":
            cmd_show_ref(args)
        case "status":
            cmd_status(args)
        case "tag":
            cmd_tag(args)
        case _:
            print("Bad command.")


def cmd_add(args):
    pass


def cmd_cat_file(args):
    pass


def cmd_check_ignore(args):
    pass


def cmd_checkout(args):
    pass


def cmd_commit(args):
    pass


def cmd_hash_object(args):
    pass


def cmd_init(args):
    pass


def cmd_log(args):
    pass


def cmd_ls_files(args):
    pass


def cmd_ls_tree(args):
    pass


def cmd_rev_parse(args):
    pass


def cmd_rm(args):
    pass


def cmd_show_ref(args):
    pass


def cmd_status(args):
    pass


def cmd_tag(args):
    pass
