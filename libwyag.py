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


class GitRepository(object):
    """A git repository"""

    worktree = None
    gitdir = None
    conf = None

    def __init__(self, path, force=False):
        self.worktree = path
        self.gitdir = os.path.join(path, ".git")

        if not (force or os.path.isdir(self.gitdir)):
            raise Exception(f"Not a Git repository {path}")

        self.conf = configparser.ConfigParser()
        cf = self.get_file("config")

        if cf and os.path.exists(cf):
            self.conf.read([cf])
        elif not force:
            raise Exception("Configuration file missing")

        if not force:
            vers = int(self.conf.get("core", "repositoryformatversion"))
            if vers != 0:
                raise Exception(f"Unsupported repositoryformatversion: {vers}")

    def get_path(self, *path):
        """Compute path under repo's gitdir"""
        return os.path.join(self.gitdir, *path)

    def get_file(self, *path, mkdir=False):
        """
        Same as get_path, but create dirname(*path) if absent.  For example,
        self.get_file(r, \"refs\", \"remotes\", \"origin\", \"HEAD\") will create
        .git/refs/remotes/origin.
        """

        if self.get_dir(*path[:-1], mkdir=mkdir):
            return self.get_path(*path)

    def get_dir(self, *path, mkdir=False):
        """Same as get_path, but mkdir *path if absent if mkdir"""

        path = self.get_path(*path)

        if os.path.exists(path):
            if os.path.isdir(path):
                return path
            else:
                raise Exception(f"Not a directory {path}")

        if mkdir:
            os.makedirs(path)
            return path
        else:
            return None

    @classmethod
    def create(cls, path):
        """Create a new repository at path"""

        repo = cls(path, True)

        if os.path.exists(repo.worktree):
            if not os.path.isdir(repo.worktree):
                raise Exception(f"{path} is not a directory!")
            if os.path.exists(repo.gitdir) and os.listdir(repo.gitdir):
                raise Exception(f"{path} is not empty!")
        else:
            os.makedirs(repo.worktree)

        assert repo.get_dir("branches", mkdir=True)
        assert repo.get_dir("objects", mkdir=True)
        assert repo.get_dir("refs", "tags", mkdir=True)
        assert repo.get_dir("refs", "heads", mkdir=True)

        with open(repo.get_file("description"), "w") as f:
            f.write(
                "Unnamed repository; edit this file 'description' to name the repository.\n"
            )

        with open(repo.get_file("HEAD"), "w") as f:
            f.write("ref: refs/heads/master\n")

        with open(repo.get_file("config"), "w") as f:
            config = cls.default_config()
            config.write(f)

        return repo

    @classmethod
    def find(cls, path=".", required=True):
        path = os.path.realpath(path)

        if os.path.isdir(os.path.join(path, ".git")):
            return cls(path)

        parent = os.path.realpath(os.path.join(path, ".."))

        if parent == path:
            if required:
                raise Exception("No git directory")
            else:
                return None

        return cls.find(parent, required)

    @staticmethod
    def default_config():
        ret = configparser.ConfigParser()

        ret.add_section("core")
        ret.set("core", "repositoryformatversion", "0")
        ret.set("core", "filemode", "false")
        ret.set("core", "bare", "false")

        return ret

    def resolve_ref(self, ref):
        path = self.get_file(ref)

        if not os.path.isfile(path):
            return None

        with open(path, "r") as fp:
            data = fp.read()[:-1]
        if data.startswith("ref: "):
            return self.resolve_ref(data[5:])
        else:
            return data

    def list_refs(self, path=None):
        if not path:
            path = self.get_dir("refs")
        ret = dict()

        for f in sorted(os.listdir(path)):
            can = os.path.join(path, f)
            if os.path.isdir(can):
                ret[f] = self.list_refs(can)
            else:
                ret[f] = self.resolve_ref(can)

        return ret

    def create_ref(self, ref_name, sha):
        with open(self.get_file("refs/" + ref_name), "w") as fp:
            fp.write(sha + "\n")

    def resolve_object(self, name):
        """
        Resolve name to an object hash in repo.
        This function is aware of:
            - the HEAD literal
            - short and long hashes
            - tags
            - branches
            - remote branches
        """
        candidates = list()
        hashRE = re.compile(r"^[0-9A-Fa-f]{4,40}$")

        if not name.strip():
            return None

        if name == "HEAD":
            return [self.resolve_ref("HEAD")]

        if hashRE.match(name):
            name = name.lower()
            prefix = name[0:2]
            path = self.get_dir("objects", prefix, mkdir=False)
            if path:
                rem = name[2:]
                for f in os.listdir(path):
                    if f.startswith(rem):
                        candidates.append(prefix + f)

        as_tag = self.resolve_ref("refs/tags/" + name)
        if as_tag:
            candidates.append(as_tag)

        as_branch = self.resolve_ref("refs/heads/" + name)
        if as_branch:
            candidates.append(as_branch)

        as_remote_branch = self.resolve_ref("refs/remotes/" + name)
        if as_remote_branch:
            candidates.append(as_remote_branch)

        return candidates

    def find_object(self, name, fmt=None, follow=True):
        sha = self.resolve_object(name)

        if not sha:
            raise Exception(f"No such reference {name}.")

        if len(sha) > 1:
            raise Exception(
                f"Ambiguous reference {name}: Candidates are:\n - {'\n - '.join(sha)}."
            )

        sha = sha[0]

        if not fmt:
            return sha

        while True:
            obj = self.read_object(sha)

            if obj.fmt == fmt:
                return sha

            if not follow:
                return None

            if obj.fmt == b"tag":
                sha = obj.kvlm[b"object"].decode("ascii")
            elif obj.fmt == b"commit" and fmt == b"tree":
                sha = obj.kvlm[b"tree"].decode("ascii")
            else:
                return None

    def read_object(self, sha):
        """
        Read object sha from Git repository repo. Return a
        GitObject whose exact type depends on the object.
        """

        path = self.get_file("objects", sha[0:2], sha[2:])

        if not os.path.isfile(path):
            return None

        with open(path, "rb") as f:
            raw = zlib.decompress(f.read())

            # Read object type
            x = raw.find(b" ")
            fmt = raw[0:x]

            # Read and validate object size
            y = raw.find(b"\x00", x)
            size = int(raw[x:y].decode("ascii"))
            if size != len(raw) - y - 1:
                raise Exception(f"Malformed object {sha}: bad length")

            # Pick constructor
            match fmt:
                case b"commit":
                    c = GitCommit
                case b"tree":
                    c = GitTree
                case b"tag":
                    c = GitTag
                case b"blob":
                    c = GitBlob
                case _:
                    raise Exception(
                        f"Unknown type {fmt.decode('ascii')} for object {sha}"
                    )

            return c(raw[y + 1 :])

    def log_graphviz(self, sha, seen):
        if sha in seen:
            return
        seen.add(sha)

        commit = self.read_object(sha)
        message = commit.kvlm[None].decode("utf8").strip()
        message = message.replace("\\", "\\\\")
        message = message.replace('"', '\\"')

        if "\n" in message:
            message = message[: message.index("\n")]

        print(f'  c_{sha} [label="{sha[0:7]}: {message}"]')
        assert commit.fmt == b"commit"

        if not b"parent" in commit.kvlm.keys():
            return

        parents = commit.kvlm[b"parent"]

        if type(parents) != list:
            parents = [parents]

        for p in parents:
            p = p.decode("ascii")
            print(f"  c_{sha} -> c_{p};")
            self.log_graphviz(p, seen)

    def show_ref(self, refs, with_hash=True, prefix=""):
        if prefix:
            prefix = prefix + "/"
        for k, v in refs.items():
            if type(v) == str and with_hash:
                print(f"{v} {prefix}{k}")
            elif type(v) == str:
                print(f"{prefix}{k}")
            else:
                self.show_ref(v, with_hash=with_hash, prefix=f"{prefix}{k}")

    def create_tag(self, name, ref, create_tag_object=False):
        sha = self.find_object(ref)

        if create_tag_object:
            tag = GitTag()
            tag.kvlm = dict()
            tag.kvlm[b"object"] = sha.encode()
            tag.kvlm[b"type"] = b"commit"
            tag.kvlm[b"tag"] = name.encode()
            tag.kvlm[b"tagger"] = b"Wyag <wyag@example.com>"
            tag.kvlm[None] = (
                b"A tag generated by wyag, which won't let you customize the message!\n"
            )
            tag_sha = tag.write(self)
            self.create_ref("tags/" + name, tag_sha)
        else:
            self.create_ref("tags/" + name, sha)


class GitObject(object):
    def __init__(self, data=None):
        if data != None:
            self.deserialize(data)
        else:
            self.init()

    def serialize(self, repo):
        """
        This function MUST be implemented by subclasses.

        It must read the object's contents from self.data, a byte string, and
        do whatever it takes to convert it into a meaningful representation.
        What exactly that means depend on each subclass
        """
        raise Exception("Unimplemented!")

    def deserialize(self, data):
        raise Exception("Unimplemented!")

    def init(self):
        pass

    def write(self, repo=None):
        # serialize object data
        data = self.serialize()
        # Add header
        result = self.fmt + b" " + str(len(data)).encode() + b"\x00" + data
        # Compute hash
        sha = hashlib.sha1(result).hexdigest()

        if repo:
            path = repo.get_file("objects", sha[0:2], sha[2:], mkdir=True)

            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(zlib.compress(result))

        return sha

    @staticmethod
    def hash(fd, fmt, repo=None):
        """Hash object, writing it to repo if provided"""
        data = fd.read()

        match fmt:
            case b"commit":
                obj = GitCommit(data)
            case b"tree":
                obj = GitTree(data)
            case b"tag":
                obj = GitTag(data)
            case b"blob":
                obj = GitBlob(data)
            case _:
                raise Exception(f"Unknown type {fmt}!")

        return obj.write(repo)


class GitBlob(GitObject):
    fmt = b"blob"

    def serialize(self):
        return self.blobdata

    def deserialize(self, data):
        self.blobdata = data


class GitCommit(GitObject):
    fmt = b"commit"

    def serialize(self):
        return self.serialize_kvlm(self.kvlm)

    def deserialize(self, data):
        self.kvlm = self.parse_kvlm(data)

    def init(self):
        self.kvlm = dict()

    @classmethod
    def parse_kvlm(cls, raw, start=0, dct=None):
        if not dct:
            dct = dict()

        # Search for the new space and the next newline
        spc = raw.find(b" ", start)
        nl = raw.find(b"\n", start)

        if spc < 0 or nl < spc:
            assert nl == start
            dct[None] = raw[start + 1 :]
            return dct

        key = raw[start:spc]
        end = start
        while True:
            end = raw.find(b"\n", end + 1)
            if raw[end + 1] != ord(" "):
                break

        value = raw[spc + 1 : end].replace(b"\n ", b"\n")

        if key in dct:
            if type(dct[key]) == list:
                dct[key].append(value)
            else:
                dct[key] = [dct[key], value]
        else:
            dct[key] = value

        return cls.parse_kvlm(raw, start=end + 1, dct=dct)

    @staticmethod
    def serialize_kvlm(kvlm):
        ret = b""

        for k in kvlm.keys():
            if k == None:
                continue
            val = kvlm[k]
            if type(val) != list:
                val = [val]

            for v in val:
                ret += k + b" " + (v.replace(b"\n", b"\n ")) + b"\n"

        ret += b"\n" + kvlm[None]

        return ret


class GitTreeLeaf(object):
    def __init__(self, mode, path, sha):
        self.mode = mode
        self.path = path
        self.sha = sha


class GitTree(GitObject):
    fmt = b"tree"

    def serialize(self):
        return self.serialize_tree(self)

    def deserialize(self, data):
        self.items = self.parse_tree(data)

    def init(self):
        self.items = list()

    @staticmethod
    def parse_one(raw, start=0):
        # Find the space terminator of the mode
        x = raw.find(b" ", start)
        assert x - start == 5 or x - start == 6

        # Read the mode
        mode = raw[start:x]
        if len(mode) == 5:
            mode = b"0" + mode

        # Read the path
        y = raw.find(b"\x00", x)
        path = raw[x + 1 : y]

        # Read the SHA
        raw_sha = int.from_bytes(raw[y + 1 : y + 21], "big")
        sha = format(raw_sha, "040x")
        return y + 21, GitTreeLeaf(mode, path.decode("utf8"), sha)

    @classmethod
    def parse_tree(cls, raw):
        pos = 0
        max = len(raw)
        ret = list()
        while pos < max:
            pos, data = cls.parse_one(raw, pos)
            ret.append(data)
        return ret

    def serialize_tree(self):
        self.items.sort(
            key=lambda leaf: (
                leaf.path + "/" if leaf.mode.startswith(b"4") else leaf.path
            )
        )
        ret = b""
        for i in self.items:
            ret += i.mode
            ret += b" "
            ret += i.path.encode("utf8")
            ret += b"\x00"
            sha = int(i.sha, 16)
            ret += sha.to_bytes(20, byteorder="big")
        return ret

    def checkout(self, repo, path):
        for item in self.items:
            obj = repo.read_object(item.sha)
            dest = os.path.join(path, item.path)

            if obj.fmt == b"tree":
                os.mkdir(dest)
                obj.checkout(repo, dest)
            elif obj.fmt == b"blob":
                with open(dest, "wb") as f:
                    f.write(obj.blobdata)

    def ls(self, repo, recursive=False, prefix=""):
        for item in self.items:
            if len(item.mode) == 5:
                type = item.mode[0:1]
            else:
                type = item.mode[0:2]

            match type:
                case b"04":
                    type = "tree"
                case b"10":
                    type = "blob"
                case b"12":
                    type = "blob"
                case b"16":
                    type = "commit"
                case _:
                    raise Exception(f"Weird tree leaf mode {item.mode}")

            if not (recursive and type == "tree"):
                mode = "0" * (6 - len(item.mode)) + item.mode.decode("ascii")
                print(f"{mode} {type} {item.sha}\t{os.path.join(prefix, item.path)}")
            else:
                obj = repo.read_object(item.sha)
                obj.ls(repo, recursive, os.path.join(prefix, item.path))


class GitTag(GitCommit):
    fmt = b"tag"


argparser = argparse.ArgumentParser(description="The stupidest content tracker")
argsubparsers = argparser.add_subparsers(title="Commands", dest="command")
argsubparsers.required = True

argsp = argsubparsers.add_parser("init", help="Initialize a new, empty repository.")
argsp.add_argument(
    "path",
    metavar="directory",
    nargs="?",
    default=".",
    help="Where to create the repository.",
)

argsp = argsubparsers.add_parser(
    "cat-file", help="Provide content of repository objects"
)
argsp.add_argument(
    "type",
    metavar="type",
    choices=["blob", "commit", "tag", "tree"],
    help="Specify the type",
)
argsp.add_argument("object", metavar="object", help="The object to display")

argsp = argsubparsers.add_parser(
    "hash-object", help="Compute object ID and optionally creates a blob from a file"
)
argsp.add_argument(
    "-t",
    metavar="type",
    dest="type",
    choices=["blob", "commit", "tag", "tree"],
    default="blob",
    help="Specify the type",
)
argsp.add_argument(
    "-w",
    dest="write",
    action="store_true",
    help="Actually write the object into the database",
)
argsp.add_argument("path", help="Read object from <file>")

argsp = argsubparsers.add_parser("log", help="Display history of a given commit.")
argsp.add_argument("commit", default="HEAD", nargs="?", help="Commit to start at.")

argsp = argsubparsers.add_parser("ls-tree", help="Pretty-print a tree object.")
argsp.add_argument(
    "-r", dest="recursive", action="store_true", help="Recurse into sub-trees"
)
argsp.add_argument("tree", help="A tree-ish object.")

argsp = argsubparsers.add_parser(
    "checkout", help="Checkout a commit inside of a directory."
)
argsp.add_argument("commit", help="The commit or tree to checkout.")
argsp.add_argument("path", help="The EMPTY directory to checkout on.")

argsp = argsubparsers.add_parser("show-ref", help="List references.")

argsp = argsubparsers.add_parser("tag", help="List and create tags")
argsp.add_argument(
    "-a",
    action="store_true",
    dest="create_tag_object",
    help="Whether to create a tag object",
)
argsp.add_argument("name", nargs="?", help="The new tag's name")
argsp.add_argument(
    "object", default="HEAD", nargs="?", help="The object the new tag will point to"
)

argsp = argsubparsers.add_parser(
    "rev-parse", help="Parse revision (or other objects) identifiers"
)
argsp.add_argument(
    "--wyag-type",
    metavar="type",
    dest="type",
    choices=["blob", "commit", "tag", "tree"],
    default=None,
    help="Specify the expected type",
)
argsp.add_argument("name", help="The name to parse")


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
    repo = GitRepository.find()
    sha = repo.find_object(args.object, fmt=args.type.encode())
    obj = repo.read_object(sha)
    sys.stdout.buffer.write(obj.serialize())


def cmd_check_ignore(args):
    pass


def cmd_checkout(args):
    repo = GitRepository.find()

    sha = repo.find_object(args.commit)
    obj = repo.read_object(sha)

    if obj.fmt == b"commit":
        obj = repo.read_object(obj.kvlm[b"tree"].decode("ascii"))

    if os.path.exists(args.path):
        if not os.path.isdir(args.path):
            raise Exception(f"Not a directory {args.path}!")
        if os.listdir(args.path):
            raise Exception(f"Not empty {args.path}!")
    else:
        os.makedirs(args.path)

    obj.checkout(repo, os.path.realpath(args.path))


def cmd_commit(args):
    pass


def cmd_hash_object(args):
    if args.write:
        repo = GitRepository.find()
    else:
        repo = None

    with open(args.path, "rb") as fd:
        sha = GitObject.hash(fd, args.type.encode(), repo)
        print(sha)


def cmd_init(args):
    GitRepository.create(args.path)


def cmd_log(args):
    repo = GitRepository.find()

    print("digraph wyaglog{")
    print("  node[shape=rect]")
    repo.log_graphviz(repo.find_object(args.commit), set())
    print("}")


def cmd_ls_files(args):
    pass


def cmd_ls_tree(args):
    repo = GitRepository.find()
    sha = repo.find_object(args.tree, fmt=b"tree")
    obj = repo.read_object(sha)
    obj.ls(repo, args.recursive)


def cmd_rev_parse(args):
    if args.type:
        fmt = args.type.encode()
    else:
        fmt = None

    repo = GitRepository.find()

    print(repo.find_object(args.name, fmt, follow=True))


def cmd_rm(args):
    pass


def cmd_show_ref(args):
    repo = GitRepository.find()
    refs = repo.list_refs()
    repo.show_ref(refs, prefix="refs")


def cmd_status(args):
    pass


def cmd_tag(args):
    repo = GitRepository.find()

    if args.name:
        repo.create_tag(args.name, args.object, create_tag_object=args.create_tag_object)
    else:
        refs = repo.list_refs()
        repo.show_ref(refs["tags"], with_hash=False)
