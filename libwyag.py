from __future__ import annotations

import argparse
import configparser
from datetime import datetime
from fnmatch import fnmatch
import hashlib
from math import ceil
import os
from os.path import relpath
import re
import sys
import zlib
from typing import Optional, List, Dict, Set, Tuple, Any, Union, BinaryIO, Type

try:
    import grp, pwd
except ModuleNotFoundError:
    pass


class GitRepository(object):
    """A git repository"""

    worktree: str
    gitdir: str
    conf: configparser.ConfigParser

    def __init__(self, path: str, force: bool = False) -> None:
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

    def get_path(self, *path: str) -> str:
        """Compute path under repo's gitdir"""
        return os.path.join(self.gitdir, *path)

    def get_file(self, *path: str, mkdir: bool = False) -> Optional[str]:
        """
        Same as get_path, but create dirname(*path) if absent.  For example,
        self.get_file(r, \"refs\", \"remotes\", \"origin\", \"HEAD\") will create
        .git/refs/remotes/origin.
        """

        if self.get_dir(*path[:-1], mkdir=mkdir):
            return self.get_path(*path)
        return None

    def get_dir(self, *path: str, mkdir: bool = False) -> Optional[str]:
        """Same as get_path, but mkdir *path if absent if mkdir"""

        full_path = self.get_path(*path)

        if os.path.exists(full_path):
            if os.path.isdir(full_path):
                return full_path
            else:
                raise Exception(f"Not a directory {full_path}")

        if mkdir:
            os.makedirs(full_path)
            return full_path
        else:
            return None

    @classmethod
    def create(cls, path: str) -> GitRepository:
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
    def find(cls, path: str = ".", required: bool = True) -> Optional[GitRepository]:
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
    def default_config() -> configparser.ConfigParser:
        ret = configparser.ConfigParser()

        ret.add_section("core")
        ret.set("core", "repositoryformatversion", "0")
        ret.set("core", "filemode", "false")
        ret.set("core", "bare", "false")

        return ret

    def resolve_ref(self, ref: str) -> Optional[str]:
        path = self.get_file(ref)

        if not path or not os.path.isfile(path):
            return None

        with open(path, "r") as fp:
            data = fp.read()[:-1]
        if data.startswith("ref: "):
            return self.resolve_ref(data[5:])
        else:
            return data

    def list_refs(self, path: Optional[str] = None) -> Dict[str, Any]:
        if not path:
            path = self.get_dir("refs")

        if not path:
            return dict()

        ret: Dict[str, Any] = dict()

        for f in sorted(os.listdir(path)):
            can = os.path.join(path, f)
            if os.path.isdir(can):
                ret[f] = self.list_refs(can)
            else:
                ret[f] = self.resolve_ref(can)

        return ret

    def create_ref(self, ref_name: str, sha: str) -> None:
        ref_path = self.get_file("refs/" + ref_name, mkdir=True)
        if ref_path:
            with open(ref_path, "w") as fp:
                fp.write(sha + "\n")

    def resolve_object(self, name: str) -> List[str]:
        """
        Resolve name to an object hash in repo.
        """
        candidates: List[str] = list()
        hashRE = re.compile(r"^[0-9A-Fa-f]{4,40}$")

        if not name.strip():
            return candidates

        if name == "HEAD":
            resolved = self.resolve_ref("HEAD")
            if resolved:
                return [resolved]

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

    def find_object(
        self, name: str, fmt: Optional[bytes] = None, follow: bool = True
    ) -> Optional[str]:
        sha_list = self.resolve_object(name)

        if not sha_list:
            raise Exception(f"No such reference {name}.")

        if len(sha_list) > 1:
            raise Exception(
                f"Ambiguous reference {name}: Candidates are:\n - {'\n - '.join(sha_list)}."
            )

        sha = sha_list[0]

        if not fmt:
            return sha

        while True:
            obj = self.read_object(sha)
            if not obj:
                return None

            if obj.fmt == fmt:
                return sha

            if not follow:
                return None

            if obj.fmt == b"tag":
                assert isinstance(obj, GitTag)
                sha = obj.kvlm[b"object"].decode("ascii")
            elif obj.fmt == b"commit" and fmt == b"tree":
                assert isinstance(obj, GitCommit)
                sha = obj.kvlm[b"tree"].decode("ascii")
            else:
                return None

    def read_object(self, sha: str) -> Optional[GitObject]:
        """
        Read object sha from Git repository repo. Return a
        GitObject whose exact type depends on the object.
        """

        path = self.get_file("objects", sha[0:2], sha[2:])

        if not path or not os.path.isfile(path):
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
            c: Type[GitObject]
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

    def log_graphviz(self, sha: str, seen: Set[str]) -> None:
        if sha in seen:
            return
        seen.add(sha)

        commit = self.read_object(sha)
        assert isinstance(commit, GitCommit)

        message_bytes: bytes = commit.kvlm[None]
        message = message_bytes.decode("utf8").strip()
        message = message.replace("\\", "\\\\")
        message = message.replace('"', '\\"')

        if "\n" in message:
            message = message[: message.index("\n")]

        print(f'  c_{sha} [label="{sha[0:7]}: {message}"]')

        if b"parent" not in commit.kvlm:
            return

        parents = commit.kvlm[b"parent"]

        if not isinstance(parents, list):
            parents = [parents]

        for p in parents:
            p_str = p.decode("ascii")
            print(f"  c_{sha} -> c_{p_str};")
            self.log_graphviz(p_str, seen)

    def show_ref(
        self, refs: Dict[str, Any], with_hash: bool = True, prefix: str = ""
    ) -> None:
        if prefix:
            prefix = prefix + "/"
        for k, v in refs.items():
            if isinstance(v, str) and with_hash:
                print(f"{v} {prefix}{k}")
            elif isinstance(v, str):
                print(f"{prefix}{k}")
            else:
                self.show_ref(v, with_hash=with_hash, prefix=f"{prefix}{k}")

    def create_tag(self, name: str, ref: str, create_tag_object: bool = False) -> None:
        sha = self.find_object(ref)
        if not sha:
            raise Exception(f"Failed to find object {ref}")

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
    fmt: bytes

    def __init__(self, data: Optional[bytes] = None) -> None:
        if data is not None:
            self.deserialize(data)
        else:
            self.init()

    def serialize(self) -> bytes:
        raise Exception("Unimplemented!")

    def deserialize(self, data: bytes) -> None:
        raise Exception("Unimplemented!")

    def init(self) -> None:
        pass

    def write(self, repo: Optional[GitRepository] = None) -> str:
        # serialize object data
        data = self.serialize()
        # Add header
        result = self.fmt + b" " + str(len(data)).encode() + b"\x00" + data
        # Compute hash
        sha = hashlib.sha1(result).hexdigest()

        if repo:
            path = repo.get_file("objects", sha[0:2], sha[2:], mkdir=True)
            if path and not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(zlib.compress(result))

        return sha

    @staticmethod
    def hash(fd: BinaryIO, fmt: bytes, repo: Optional[GitRepository] = None) -> str:
        """Hash object, writing it to repo if provided"""
        data = fd.read()
        obj: GitObject

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
                raise Exception(f"Unknown type {fmt.decode()}!")

        return obj.write(repo)


class GitBlob(GitObject):
    fmt = b"blob"
    blobdata: bytes

    def serialize(self) -> bytes:
        return self.blobdata

    def deserialize(self, data: bytes) -> None:
        self.blobdata = data


class GitCommit(GitObject):
    fmt = b"commit"
    kvlm: Dict[Optional[bytes], Any]

    def serialize(self) -> bytes:
        return self.serialize_kvlm(self.kvlm)

    def deserialize(self, data: bytes) -> None:
        self.kvlm = self.parse_kvlm(data)

    def init(self) -> None:
        self.kvlm = dict()

    @classmethod
    def parse_kvlm(
        cls,
        raw: bytes,
        start: int = 0,
        dct: Optional[Dict[Optional[bytes], Any]] = None,
    ) -> Dict[Optional[bytes], Any]:
        if dct is None:
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
            if raw[end + 1] != ord(b" "):
                break

        value = raw[spc + 1 : end].replace(b"\n ", b"\n")

        if key in dct:
            if isinstance(dct[key], list):
                dct[key].append(value)
            else:
                dct[key] = [dct[key], value]
        else:
            dct[key] = value

        return cls.parse_kvlm(raw, start=end + 1, dct=dct)

    @staticmethod
    def serialize_kvlm(kvlm: Dict[Optional[bytes], Any]) -> bytes:
        ret = b""

        for k in kvlm.keys():
            if k is None:
                continue
            val = kvlm[k]
            if not isinstance(val, list):
                val = [val]

            for v in val:
                ret += k + b" " + (v.replace(b"\n", b"\n ")) + b"\n"

        ret += b"\n" + kvlm[None]

        return ret


class GitTreeLeaf(object):
    mode: bytes
    path: str
    sha: str

    def __init__(self, mode: bytes, path: str, sha: str) -> None:
        self.mode = mode
        self.path = path
        self.sha = sha


class GitTree(GitObject):
    fmt = b"tree"
    items: List[GitTreeLeaf]

    def serialize(self) -> bytes:
        return self.serialize_tree()

    def deserialize(self, data: bytes) -> None:
        self.items = self.parse_tree(data)

    def init(self) -> None:
        self.items = list()

    @staticmethod
    def parse_one(raw: bytes, start: int = 0) -> Tuple[int, GitTreeLeaf]:
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
    def parse_tree(cls, raw: bytes) -> List[GitTreeLeaf]:
        pos = 0
        max_len = len(raw)
        ret: List[GitTreeLeaf] = list()
        while pos < max_len:
            pos, data = cls.parse_one(raw, pos)
            ret.append(data)
        return ret

    def serialize_tree(self) -> bytes:
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

    def checkout(self, repo: GitRepository, path: str) -> None:
        for item in self.items:
            obj = repo.read_object(item.sha)
            if not obj:
                raise Exception(f"Object {item.sha} not found")

            dest = os.path.join(path, item.path)

            if obj.fmt == b"tree":
                assert isinstance(obj, GitTree)
                os.mkdir(dest)
                obj.checkout(repo, dest)
            elif obj.fmt == b"blob":
                assert isinstance(obj, GitBlob)
                with open(dest, "wb") as f:
                    f.write(obj.blobdata)

    def ls(
        self, repo: GitRepository, recursive: bool = False, prefix: str = ""
    ) -> None:
        for item in self.items:
            if len(item.mode) == 5:
                type_str = item.mode[0:1]
            else:
                type_str = item.mode[0:2]

            match type_str:
                case b"04":
                    type_str_decoded = "tree"
                case b"10":
                    type_str_decoded = "blob"
                case b"12":
                    type_str_decoded = "blob"
                case b"16":
                    type_str_decoded = "commit"
                case _:
                    raise Exception(f"Weird tree leaf mode {item.mode!r}")

            if not (recursive and type_str_decoded == "tree"):
                mode = "0" * (6 - len(item.mode)) + item.mode.decode("ascii")
                print(
                    f"{mode} {type_str_decoded} {item.sha}\t{os.path.join(prefix, item.path)}"
                )
            else:
                obj = repo.read_object(item.sha)
                assert isinstance(obj, GitTree)
                obj.ls(repo, recursive, os.path.join(prefix, item.path))


class GitTag(GitCommit):
    fmt = b"tag"


class GitIndexEntry(object):
    def __init__(
        self,
        ctime=None,
        mtime=None,
        dev=None,
        ino=None,
        mode_type=None,
        mode_perms=None,
        uid=None,
        gid=None,
        fsize=None,
        sha=None,
        flag_assume_valid=None,
        flag_stage=None,
        name=None,
    ):
        # The last time a file's metadata changed.
        self.ctime = ctime
        # The last time a file's data changed.
        self.mtime = mtime
        # The ID of device containing this file
        self.dev = dev
        # The file's inode number
        self.ino = ino
        # The object type, either b1000 (regular), b1010 (symlink),
        # b1110 (gitlink).
        self.mode_type = mode_type
        # The object permissions, an integer.
        self.mode_perms = mode_perms
        # User ID of owner
        self.uid = uid
        # Group ID of ownner
        self.gid = gid
        # Size of this object, in bytes
        self.fsize = fsize
        # The object's SHA
        self.sha = sha
        self.flag_assume_valid = flag_assume_valid
        self.flag_stage = flag_stage
        # Name of the object (full path this time!)
        self.name = name


class GitIndex(object):
    version = None
    entries = []

    def __init__(self, version=2, entries=None):
        if not entries:
            entries = list()

        self.version = version
        self.entries = entries


def index_read(repo):
    index_file = repo.get_file("index")

    if not os.path.exists(index_file):
        return GitIndex()

    with open(index_file, "rb") as f:
        raw = f.read()

    header = raw[:12]
    signature = header[:4]
    assert signature == b"DIRC"
    version = int.from_bytes(header[4:8], "big")
    assert version == 2, "wyag only supports index file version 2"
    count = int.from_bytes(header[8:12], "big")

    entries = list()

    content = raw[12:]
    idx = 0
    for i in range(0, count):
        # Read creation time, as a unix timestamp
        ctime_s = int.from_bytes(content[idx : idx + 4], "big")
        # Read creation time, as nanoseconds after that timestamps
        ctime_ns = int.from_bytes(content[idx + 4 : idx + 8], "big")
        # Same for modification time: first seconds from epoch
        mtime_s = int.from_bytes(content[idx + 8 : idx + 12], "big")
        # Then extra nanoseconds
        mtime_ns = int.from_bytes(content[idx + 12 : idx + 16], "big")
        # Device ID
        dev = int.from_bytes(content[idx + 16 : idx + 20], "big")
        # Inode
        ino = int.from_bytes(content[idx + 20 : idx + 24], "big")
        # Ignored
        unused = int.from_bytes(content[idx + 24 : idx + 26], "big")
        assert unused == 0
        mode = int.from_bytes(content[idx + 26 : idx + 28], "big")
        mode_type = mode >> 12
        assert mode_type in [0b1000, 0b1010, 0b1110]
        mode_perms = mode & 0b0000000111111111
        # User ID
        uid = int.from_bytes(content[idx + 28 : idx + 32], "big")
        # Group ID
        gid = int.from_bytes(content[idx + 32 : idx + 36], "big")
        # Size
        fsize = int.from_bytes(content[idx + 36 : idx + 40], "big")
        # SHA (object ID).
        sha = format(int.from_bytes(content[idx + 40 : idx + 60], "big"), "040x")
        # Flags we're going to ignore
        flags = int.from_bytes(content[idx + 60 : idx + 62], "big")
        # Parse flags
        flag_assume_valid = (flags & 0b1000000000000000) != 0
        flag_extended = (flags & 0b0100000000000000) != 0
        assert not flag_extended
        flag_stage = flags & 0b0011000000000000
        # Length of the name
        name_length = flags & 0b0000111111111111

        idx += 62

        if name_length < 0xFFF:
            assert content[idx + name_length] == 0x00
            raw_name = content[idx : idx + name_length]
            idx += name_length + 1
        else:
            print(f"Notice: Name is 0x{name_length:X} bytes long.")
            null_idx = content.find(b"\x00", idx + 0xFFF)
            raw_name = content[idx:null_idx]
            idx = null_idx + 1

        name = raw_name.decode("utf8")

        idx = 8 * ceil(idx / 8)

        entries.append(
            GitIndexEntry(
                ctime=(ctime_s, ctime_ns),
                mtime=(mtime_s, mtime_ns),
                dev=dev,
                ino=ino,
                mode_type=mode_type,
                mode_perms=mode_perms,
                uid=uid,
                gid=gid,
                fsize=fsize,
                sha=sha,
                flag_assume_valid=flag_assume_valid,
                flag_stage=flag_stage,
                name=name,
            )
        )

    return GitIndex(version=version, entries=entries)


def index_write(repo, index):
    with open(repo.get_file("index"), "wb") as f:
        # HEADER
        f.write(b"DIRC")
        f.write(index.version.to_bytes(4, "big"))
        f.write(len(index.entries).to_bytes(4, "big"))

        # ENTRIES
        idx = 0
        for e in index.entries:
            f.write(e.ctime[0].to_bytes(4, "big"))
            f.write(e.ctime[1].to_bytes(4, "big"))
            f.write(e.mtime[0].to_bytes(4, "big"))
            f.write(e.mtime[1].to_bytes(4, "big"))
            f.write(e.dev.to_bytes(4, "big"))
            f.write(e.ino.to_bytes(4, "big"))

            # Mode
            mode = (e.mode_type << 12) | e.mode_perms
            f.write(mode.to_bytes(4, "big"))

            f.write(e.uid.to_bytes(4, "big"))
            f.write(e.gid.to_bytes(4, "big"))

            f.write(e.fsize.to_bytes(4, "big"))
            f.write(int(e.sha, 16).to_bytes(20, "big"))

            flag_assume_valid = 0x1 << 15 if e.flag_assume_valid else 0

            name_bytes = e.name.encode("utf8")
            bytes_len = len(name_bytes)
            if bytes_len >= 0xFFF:
                name_length = 0xFFF
            else:
                name_length = bytes_len

            f.write((flag_assume_valid | e.flag_stage | name_length).to_bytes(2, "big"))

            f.write(name_bytes)
            f.write((0).to_bytes(1, "big"))

            idx += 62 + len(name_bytes) + 1

            if idx % 8 != 0:
                pad = 8 - (idx % 8)
                f.write((0).to_bytes(pad, "big"))
                idx += pad


class GitIgnore(object):
    absolute = None
    scoped = None

    def __init__(self, absolute, scoped):
        self.absolute = absolute
        self.scoped = scoped


def gitignore_parse_one(raw):
    raw = raw.strip()

    if not raw or raw[0] == "#":
        return None
    if raw[0] == "!":
        return (raw[1:], False)
    if raw[0] == "\\":
        return (raw[1:], True)
    return (raw, True)


def gitignore_parse(lines):
    ret = list()

    for line in lines:
        parsed = gitignore_parse_one(line)
        if parsed:
            ret.append(parsed)

    return ret


def gitignore_read(repo):
    ret = GitIgnore(absolute=list(), scoped=dict())

    repo_file = os.path.join(repo.gitdir, "info/exclude")
    if os.path.exists(repo_file):
        with open(repo_file, "r") as f:
            ret.absolute.append(gitignore_parse(f.readlines()))

    if "XDG_CONFIG_HOME" in os.environ:
        config_home = os.environ["XDG_CONFIG_HOME"]
    else:
        config_home = os.path.expanduser("~/.config")
    global_file = os.path.join(config_home, "git/ignore")

    if os.path.exists(global_file):
        with open(global_file, "r") as f:
            ret.absolute.append(gitignore_parse(f.readlines()))

    index = index_read(repo)
    for entry in index.entries:
        if entry.name == ".gitignore" or entry.name.endswith("/.gitignore"):
            dir_name = os.path.dirname(entry.name)
            contents = repo.read_object(entry.sha)
            lines = contents.blobdata.decode("utf8").splitlines()
            ret.scoped[dir_name] = gitignore_parse(lines)

    return ret


def check_ignore_one(rules, path):
    result = None
    for pattern, value in rules:
        if fnmatch(path, pattern):
            result = value
    return result


def check_ignore_scoped(rules, path):
    parent = os.path.dirname(path)
    while True:
        if parent in rules:
            result = check_ignore_one(rules[parent], path)
            if result != None:
                return result
        if parent == "":
            break
        parent = os.path.dirname(parent)
    return None


def check_ignore_absolute(rules, path):
    parent = os.path.dirname(path)
    for ruleset in rules:
        result = check_ignore_one(ruleset, path)
        if result != None:
            return result
    return False


def check_ignore(rules, path):
    if os.path.isabs(path):
        raise Exception(
            "This function requires path to be relative to the repository's root"
        )

    result = check_ignore_scoped(rules.scoped, path)
    if result != None:
        return result

    return check_ignore_absolute(rules.absolute, path)


def gitconfig_read():
    xdg_config_home = (
        os.environ["XDG_CONFIG_HOME"]
        if "XDG_CONFIG_HOME" in os.environ
        else "~/.config"
    )
    configfiles = [
        os.path.expanduser(os.path.join(xdg_config_home, "git/config")),
        os.path.expanduser("~/.gitconfig"),
    ]

    config = configparser.ConfigParser()
    config.read(configfiles)
    return config


def gitconfig_user_get(config):
    if "user" in config:
        if "name" in config["user"] and "email" in config["user"]:
            return f"{config['user']['name']} <{config['user']['email']}>"
    return None


def tree_from_index(repo, index):
    contents = dict()
    contents[""] = list()

    for entry in index.entries:
        dirname = os.path.dirname(entry.name)

        key = dirname
        while key != "":
            if not key in contents:
                contents[key] == list()
            key = os.path.dirname(key)

        contents[dirname].append(entry)

    sorted_paths = sorted(contents.keys(), key=len, reverse=True)
    sha = None

    for path in sorted_paths:
        tree = GitTree()

        for entry in contents[path]:
            if isinstance(entry, GitIndexEntry):
                leaf_mode = f"{entry.mode_type:02o}{entry.mode_perms:04o}".encode(
                    "ascii"
                )
                leaf = GitTreeLeaf(
                    mode=leaf_mode, path=os.path.basename(entry.name), sha=entry.sha
                )
            else:
                leaf = GitTreeLeaf(mode=b"040000", path=entry[0], sha=entry[1])

            tree.items.append(leaf)

        sha = tree.write(repo)

        parent = os.path.dirname(path)
        base = os.path.basename(path)
        contents[parent].append((base, sha))

    return sha


def commit_create(repo, tree, parent, author, timestamp, message):
    commit = GitCommit()
    commit.kvlm[b"tree"] = tree.encode("ascii")
    if parent:
        commit.kvlm[b"parent"] = parent.encode("ascii")

    message = message.strip() + "\n"
    offset = int(timestamp.astimezone().utcoffset().total_seconds())
    hours = offset // 3600
    minutes = (offset % 3600) // 60
    tz = "{}{:02}{:02}".format("+" if offset > 0 else "-", hours, minutes)

    author = author + timestamp.strftime(" %s ") + tz
    commit.kvlm[b"author"] = author.encode("utf8")
    commit.kvlm[b"committer"] = author.encode("utf8")
    commit.kvlm[None] = message.encode("utf8")

    return commit.write(repo)


# CLI definition
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

argsp = argsubparsers.add_parser("ls-files", help="List all the stage files")
argsp.add_argument("--verbose", action="store_true", help="Show everything.")

argsp = argsubparsers.add_parser(
    "check-ignore", help="Check path(s) against ignore rules."
)
argsp.add_argument("path", nargs="+", help="Paths to check")

argsp = argsubparsers.add_parser("status", help="Show the working tree status.")

argsp = argsubparsers.add_parser(
    "rm", help="Remove files from the working tree and the index."
)
argsp.add_argument("path", nargs="+", help="Files to remove")

argsp = argsubparsers.add_parser("add", help="Add files contents to the index.")
argsp.add_argument("path", nargs="+", help="Files to add")

argsp = argsubparsers.add_parser("commit", help="Record changes to the repository.")
argsp.add_argument(
    "-m",
    metavar="message",
    dest="message",
    help="Message to associate with this commit.",
)


def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
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


def cmd_add(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    add(repo, args.path)


def add(repo, paths, delete=True, skip_missing=False):
    rm(repo, paths, delete=False, skip_missing=True)

    worktree = repo.worktree + os.sep

    clean_paths = set()
    for path in paths:
        abspath = os.path.abspath(path)
        if not (abspath.startswith(worktree) and os.path.isfile(abspath)):
            raise Exception(f"Not a file, or outside the worktree: {paths}")
        relpath = os.path.relpath(abspath, repo.worktree)
        clean_paths.add((abspath, relpath))

    index = index_read(repo)

    for abspath, relpath in clean_paths:
        with open(abspath, "rb") as fd:
            sha = GitObject.hash(fd, b"blob", repo)

            stat = os.stat(abspath)

            ctime_s = int(stat.st_ctime)
            ctime_ns = stat.st_ctime_ns % 10**9
            mtime_s = int(stat.st_mtime)
            mtime_ns = stat.st_mtime_ns % 10**9

            entry = GitIndexEntry(
                ctime=(ctime_s, ctime_ns),
                mtime=(mtime_s, mtime_ns),
                dev=stat.st_dev,
                ino=stat.st_ino,
                mode_type=0b1000,
                mode_perms=0o644,
                uid=stat.st_uid,
                gid=stat.st_gid,
                fsize=stat.st_size,
                sha=sha,
                flag_assume_valid=False,
                flag_stage=False,
                name=relpath,
            )
            index.entries.append(entry)

    index_write(repo, index)


def cmd_cat_file(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    if not repo:
        raise Exception("Not in a git repository")
    sha = repo.find_object(args.object, fmt=args.type.encode())
    if sha:
        obj = repo.read_object(sha)
        if obj:
            sys.stdout.buffer.write(obj.serialize())


def cmd_check_ignore(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    rules = gitignore_read(repo)
    for path in args.path:
        if check_ignore(rules, path):
            print(path)


def cmd_checkout(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    if not repo:
        raise Exception("Not in a git repository")

    sha = repo.find_object(args.commit)
    if not sha:
        raise Exception(f"Commit {args.commit} not found")

    obj = repo.read_object(sha)
    if not obj:
        raise Exception("Failed to read object")

    if obj.fmt == b"commit":
        assert isinstance(obj, GitCommit)
        tree_sha = obj.kvlm[b"tree"].decode("ascii")
        obj = repo.read_object(tree_sha)

    assert isinstance(obj, GitTree)

    if os.path.exists(args.path):
        if not os.path.isdir(args.path):
            raise Exception(f"Not a directory {args.path}!")
        if os.listdir(args.path):
            raise Exception(f"Not empty {args.path}!")
    else:
        os.makedirs(args.path)

    obj.checkout(repo, os.path.realpath(args.path))


def cmd_commit(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    index = index_read(repo)
    tree = tree_from_index(repo, index)

    try:
        parent = repo.find_object("HEAD")
    except Exception:
        parent = None

    commit = commit_create(
        repo,
        tree,
        parent,
        gitconfig_user_get(gitconfig_read()),
        datetime.now(),
        args.message,
    )

    active_branch = branch_get_active(repo)
    if active_branch:
        with open(repo.get_file(os.path.join("refs/heads", active_branch)), "w") as fd:
            fd.write(commit + "\n")
    else:
        with open(repo.get_file("HEAD"), "w") as fd:
            fd.write("\n")


def cmd_hash_object(args: argparse.Namespace) -> None:
    repo: Optional[GitRepository] = None
    if args.write:
        repo = GitRepository.find()

    with open(args.path, "rb") as fd:
        sha = GitObject.hash(fd, args.type.encode(), repo)
        print(sha)


def cmd_init(args: argparse.Namespace) -> None:
    GitRepository.create(args.path)


def cmd_log(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    if not repo:
        raise Exception("Not in a git repository")

    sha = repo.find_object(args.commit)
    if not sha:
        raise Exception(f"Commit {args.commit} not found")

    print("digraph wyaglog{")
    print("  node[shape=rect]")
    repo.log_graphviz(sha, set())
    print("}")


def cmd_ls_files(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    index = index_read(repo)
    if args.verbose:
        print(
            f"Index file format v{index.version}, containing {len(index.entries)} entries."
        )

    for e in index.entries:
        print(e.name)
        if args.verbose:
            entry_type = {
                0b1000: "regular_file",
                0b1010: "symlink",
                0b1110: "git link",
            }[e.mode_type]
            print(f"  {entry_type} with perms: {e.mode_perms:o}")
            print(f"  on blob: {e.sha}")
            print(
                f"  created: {datetime.fromtimestamp(e.ctime[0])}.{e.ctime[1]}, modified: {datetime.fromtimestamp(e.mtime[0])}.{e.mtime[1]}"
            )
            print(f"  device: {e.dev}, inode: {e.ino}")
            try:
                print(
                    f"  user: {pwd.getpwuid(e.uid).pw_name} ({e.uid})  group: {grp.getgrgid(e.gid).gr_name} ({e.gid})"
                )
            except NameError:
                print(f"  user: {e.uid}  group: {e.gid}")


def cmd_ls_tree(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    if not repo:
        raise Exception("Not in a git repository")

    sha = repo.find_object(args.tree, fmt=b"tree")
    if sha:
        obj = repo.read_object(sha)
        assert isinstance(obj, GitTree)
        obj.ls(repo, args.recursive)


def cmd_rev_parse(args: argparse.Namespace) -> None:
    fmt: Optional[bytes] = args.type.encode() if args.type else None
    repo = GitRepository.find()
    if not repo:
        raise Exception("Not in a git repository")

    print(repo.find_object(args.name, fmt, follow=True))


def cmd_rm(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    rm(repo, args.path)


def rm(repo, paths, delete=True, skip_missing=False):
    index = index_read(repo)
    worktree = repo.worktree + os.sep

    abspaths = set()
    for path in paths:
        abspath = os.path.abspath(path)
        if abspath.startswith(worktree):
            abspaths.add(abspath)
        else:
            raise Exception(f"Cannot remove paths outside of worktree: {paths}")

    keep_entries = list()
    remove = list()

    for e in index.entries:
        full_path = os.path.join(repo.worktree, e.name)

        if full_path in abspaths:
            remove.append(full_path)
            abspaths.remove(full_path)
        else:
            keep_entries.append(e)

    if len(abspaths) > 0 and not skip_missing:
        raise Exception(f"Cannot remove paths not in the index: {abspaths}")

    if delete:
        for path in remove:
            os.unlink(path)

    index.entries = keep_entries
    index_write(repo, index)


def cmd_show_ref(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    if not repo:
        raise Exception("Not in a git repository")

    refs = repo.list_refs()
    repo.show_ref(refs, prefix="refs")


def cmd_status(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    index = index_read(repo)

    cmd_status_branch(repo)
    cmd_status_head_index(repo, index)
    print()
    cmd_status_index_worktree(repo, index)


def branch_get_active(repo):
    with open(repo.get_file("HEAD"), "r") as f:
        head = f.read()

    if head.startswith("ref: refs/heads/"):
        return head[16:-1]
    else:
        return False


def cmd_status_branch(repo):
    branch = branch_get_active(repo)
    if branch:
        print(f"On branch {branch}.")
    else:
        print(f"HEAD detached at {repo.find_object('HEAD')}")


def tree_to_dict(repo, ref, prefix=""):
    ret = dict()
    tree_sha = repo.find_object(ref, fmt=b"tree")
    tree = repo.read_object(tree_sha)

    for leaf in tree.items:
        full_path = os.path.join(prefix, leaf.path)
        is_subtree = leaf.mode.startswith(b"04")
        if is_subtree:
            ret.update(tree_to_dict(repo, leaf.sha, full_path))
        else:
            ret[full_path] = leaf.sha
    return ret


def cmd_status_head_index(repo, index):
    print("Changes to be committed:")

    head = tree_to_dict(repo, "HEAD")
    for entry in index.entries:
        if entry.name in head:
            if head[entry.name] != entry.sha:
                print("  modified:", entry.name)
            del head[entry.name]
        else:
            print("  added:   ", entry.name)

    for entry in head.keys():
        print("  deleted: ", entry)


def cmd_status_index_worktree(repo, index):
    print("Changes not staged for commit:")

    ignore = gitignore_read(repo)

    gitdir_prefix = repo.gitdir + os.path.sep

    all_files = list()

    for root, _, files in os.walk(repo.worktree, True):
        if root == repo.gitdir or root.startswith(gitdir_prefix):
            continue
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, repo.worktree)
            all_files.append(rel_path)

    for entry in index.entries:
        full_path = os.path.join(repo.worktree, entry.name)

        if not os.path.exists(full_path):
            print("  deleted: ", entry.name)
        else:
            stat = os.stat(full_path)

            ctime_ns = entry.ctime[0] * 10**9 + entry.ctime[1]
            mtime_ns = entry.mtime[0] * 10**9 + entry.mtime[1]
            if stat.st_ctime_ns != ctime_ns or stat.st_mtime_ns != mtime_ns:
                with open(full_path, "rb") as fd:
                    new_sha = GitObject.hash(fd, b"blob", None)
                    if entry.sha != new_sha:
                        print("  modified:", entry.name)

        if entry.name in all_files:
            all_files.remove(entry.name)

    print("\nUntracked files:")

    for f in all_files:
        if not check_ignore(ignore, f):
            print(" ", f)


def cmd_tag(args: argparse.Namespace) -> None:
    repo = GitRepository.find()
    if not repo:
        raise Exception("Not in a git repository")

    if args.name:
        repo.create_tag(
            args.name, args.object, create_tag_object=args.create_tag_object
        )
    else:
        refs = repo.list_refs()
        if "tags" in refs:
            repo.show_ref(refs["tags"], with_hash=False)
