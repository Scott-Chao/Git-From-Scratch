# WYAG (Write Yourself A Git) Learning Notes: Exploring the Inner Workings of Git 🛠️

## 1. Project Introduction

**What is WYAG?**
In our daily development, we type `git add` and `git commit` every day, but how does Git actually work under the hood? Is it merely a tool for comparing file differences? To demystify this "black box," this project re-implements the core functionality of Git from scratch using Python 3 (WYAG: Write Yourself a Git). By stripping away complex peripheral features like network synchronization, we dive straight into the heart of Git's architecture.

**Development Environment**:
This project was developed on **WSL2 (Ubuntu)**. Since Git relies heavily on Unix-like file system features (such as file permission Modes, soft links, Inodes, and various timestamps), using a native Linux environment provides the most authentic way to handle low-level state acquisition and the structure of the staging area.

**Core Objectives**:
To thoroughly understand the design philosophy of **Content-addressable storage** and the most complex, exquisite design in Git's architecture—the binary essence of the **Staging Area (Index)**.

---

## 2. The Soul of Git: The Object Model

> This section reveals how data is compressed, hashed, and quietly stored in the `.git/objects` directory.

### Content-addressable Storage
In traditional systems, files are located via "paths and filenames." However, in Git's underlying universe, **filenames do not matter; the content's fingerprint (SHA-1 hash) is the unique identifier**. As long as two files have identical content, even if they reside in different directories or have different names, Git will only store one copy of the data at the lower level.

### The Four Base Object Types
The world of Git is built from four fundamental building blocks:
*   **Blob (Data Object)**: A pure data container that only saves file content. It doesn't know its own name or its permissions.
*   **Tree (Tree Object)**: A binary snapshot of a directory structure. It is a mapping table that records: `Permission Mode + File/Directory Name -> Corresponding Blob/Tree Hash`.
*   **Commit (Commit Object)**: A pointer with metadata. It contains a hash pointing to a top-level Tree, hashes pointing to parent Commits, the author, timestamps, and the commit message (stored in KVLM format).
*   **Tag (Tag Object)**: A permanent alias for an object (usually a Commit), accompanied by tagger information and extra comments.

### Technical Implementation Details
Regardless of the object type, the logic for storing it in `.git/objects` is unified:
1.  **Prepend Header**: `Object type + space + content length in bytes + \x00 (null character)`.
2.  **Concatenate**: Combine the Header with the actual Payload.
3.  **Hash**: Calculate the **SHA-1** hash of the complete data (a 40-character hex string).
4.  **Compress**: Use **zlib** to compress the data.
5.  **Write to Disk**: Use the first 2 characters of the hash as the directory name and the remaining 38 characters as the filename.

---

## 3. Refs & Resolvers

> Why can you find a target using `master`, `HEAD`, or even just the first 7 digits of a hash?

### What is a Reference?
Branches and tags sound sophisticated, but if you open the `.git/refs/` directory, you'll find they are simply **plain text files** containing a 40-character hash string. Creating a new branch is essentially creating an extremely "cheap" file of a few dozen bytes.

### Symbolic Refs
`HEAD` is a special "cursor." If you are on the `master` branch, the content of the `HEAD` file is not a hash, but `ref: refs/heads/master`. This creates a resolution chain: `HEAD -> Branch Ref -> Commit Object -> Tree Object -> Blob Object`.

### Reference Discovery Logic
This project implements a flexible resolver similar to Git. When you input a name, the system attempts to resolve it based on the following priority:
1.  Exact match with `.git/HEAD`.
2.  Check if it's a valid **short hash** (regex match and prefix search in the `objects` directory).
3.  Check if it's a tag: `refs/tags/<name>`.
4.  Check if it's a local branch: `refs/heads/<name>`.
5.  Check if it's a remote branch: `refs/remotes/<name>`.

---

## 4. Secrets of the Staging Area: Deep Dive into the Index

> This is the most hardcore, low-level part of the project. `.git/index` is not a temporary folder; it is a highly compressed and optimized binary registry.

### The Role of the Index
Why doesn't Git commit workspace changes directly? The Index provides a "middle-state buffer," allowing developers to pick and choose specific changes to carefully construct the next commit snapshot.

### Binary Format Parsing (Version 2)
Parsing this file in Python requires heavy use of `int.from_bytes` and bitwise operations. Its structure is incredibly compact:
*   **Header**: Starts with the magic signature `DIRC` (Directory Cache), followed by a 4-byte version number (usually 2) and a 4-byte total count of entries.
*   **Entries**: For every file in the workspace, it stores detailed OS-level Stat information: `ctime` (creation time and nanoseconds), `mtime` (modification time and nanoseconds), `dev` (device), `ino` (Inode), `mode` (file type and permissions), `uid`, `gid`, `size`, and most importantly, the `SHA-1`.
*   **Padding/Alignment**: For efficient memory-mapped (mmap) reading, each entry is strictly padded with `\x00` to align with 8-byte boundaries.

### Extreme Performance Optimization (The principle of `git status`)
Why is `git status` so fast even in massive projects?
Because the Index stores the `mtime`, `ctime`, and `size` from the last time it was staged. Git uses the operating system's `stat` system call to perform a simple numerical comparison between the file's current state and the records in the Index. **If the timestamps and size match perfectly, Git skips the time-consuming hash calculation and assumes the file is unchanged.**

---

## 5. Ignore Mechanism & Pattern Matching (GitIgnore)

To build a usable client, a `.gitignore` mechanism is essential. This project implements rule priority and recursive matching logic consistent with Git.

*   **Multi-level Ignoring**: Global ignore (`~/.config/git/ignore` or linked via `~/.gitconfig`), repository-level absolute ignore (`.git/info/exclude`), and **dynamic local `.gitignore`** files.
*   **Recursion & Proximity**: The program starts from the directory where a file is located and moves upwards (`os.path.dirname`) to find the nearest `.gitignore` rules, utilizing Python's `fnmatch` for wildcard matching. Rules prefixed with `!` also perform negation (un-ignoring) operations.

---

## 6. Pitfalls & Technical Takeaways

*   **Binary vs. Text: A History of Blood and Tears**: In Python 3, `str` and `bytes` are strictly separated. Git's underlying files are purely binary. Tree objects contain both plain-text filenames (UTF-8) immediately followed by binary SHA-1 hashes (not readable hex, but the actual 20-byte raw numbers). Precise slicing and addressing during serialization and parsing proved to be highly challenging.
*   **WSL2 Compatibility Advantages**: Developing in a native Windows environment would make handling file permissions (Modes like `0o644`, `0o755`) and owners (UID/GID) very strange and prone to distortion. By relying on WSL2, `os.stat` in the code behaves exactly as it would on a native Linux server.
*   **The Art of Ubiquitous Recursion**:
    *   **Building Trees**: From a flat Index list (`GitTree.from_index`), the program merges paths by depth to recursively generate nested GitTrees.
    *   **Parsing KVLM**: In Commit history, parsing multi-line key-value pairs (e.g., multi-line commit messages, PGP signatures) utilizes recursion to perfectly handle unknown line counts and indentation alignments.

---

## 7. How to Run

You can clone this project and run it directly using Python 3. The command structure of this tool maintains high consistency with native Git:

```bash
# 1. Initialize an empty repository (generates full .git directory structure)
python3 ./wyag init my_test_repo
cd my_test_repo

# 2. Create a file and add it to the staging area (triggers Index write and Blob generation)
echo "Write my own Git!" > hello.txt
python3 ../wyag add hello.txt

# 3. Generate a commit tree and Commit
python3 ../wyag commit -m "First commit from WYAG"

# 4. Check status (compares differences between workspace, Index, and repository)
python3 ../wyag status

# 5. Low-level exploration: View the content of the Commit object we just generated
python3 ../wyag cat-file commit HEAD

# 6. Generate a Graphviz history relationship diagram (The "Black Tech")
python3 ../wyag log HEAD > history.dot
dot -Tpng history.dot -o history.png
# (Then you can open history.png to view the visual structure of branches and commits)
```

> **Acknowledgments**: The code structure is inspired by the [Write yourself a Git!](https://wyag.thb.lt/) tutorial, with significant Python 3 Type Hint refactoring and engineering adjustments added on top.
