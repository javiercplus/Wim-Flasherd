#!/usr/bin/env python3
"""
winusb.py - Create a bootable Windows installation USB drive from Linux.

Steps automated by this tool:
  1. Detect connected storage devices, then identify and unmount the target
  2. Partition (MBR, one primary NTFS partition, bootable flag) and format it
  3. Write boot records with ms-sys (MBR and PBR)
  4. Mount the ISO and copy its contents to the USB
  5. Sync and clean up

WARNING: this tool ERASES the selected device completely.
Writing to a device requires root (sudo python3 winusb.py ...).
Listing devices (--list) does not require root.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REQUIRED_TOOLS = ["lsblk", "sfdisk", "wipefs", "mkfs.ntfs", "ms-sys",
                  "mount", "umount", "sync"]

# Column sets for lsblk, from newest to oldest util-linux versions.
# Older versions do not know MOUNTPOINTS (plural) or PATH, so we fall back.
LSBLK_COLUMN_SETS = [
    "NAME,PATH,SIZE,MODEL,TRAN,RM,TYPE,FSTYPE,LABEL,MOUNTPOINTS",
    "NAME,PATH,SIZE,MODEL,TRAN,RM,TYPE,FSTYPE,LABEL,MOUNTPOINT",
    "NAME,SIZE,MODEL,TRAN,RM,TYPE,FSTYPE,LABEL,MOUNTPOINT",
    "NAME,SIZE,MODEL,RM,TYPE,FSTYPE,LABEL,MOUNTPOINT",
]

# Mount points that indicate the disk holds the running system
PROTECTED_MOUNTS = ("/", "/boot", "/boot/efi", "/home", "/usr", "/var", "[SWAP]")


class StepError(Exception):
    """Raised when a step of the process fails."""


def log(message):
    """Print a progress message."""
    print(f"[*] {message}")


def run(cmd, dry_run=False, check=True):
    """Run a command, printing it first. In dry-run mode nothing is executed."""
    print("    $ " + " ".join(cmd))
    if dry_run:
        return None
    result = subprocess.run(cmd, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise StepError(
            f"Command failed: {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result


def check_environment():
    """Verify root privileges and that all required tools are installed."""
    if os.geteuid() != 0:
        sys.exit("This tool must be run as root (use sudo).")
    missing = [t for t in REQUIRED_TOOLS if shutil.which(t) is None]
    if missing:
        print("Missing required tools: " + ", ".join(missing))
        print("Hints:")
        print("  - mkfs.ntfs is provided by the ntfs-3g package")
        print("  - sfdisk and wipefs come from util-linux")
        print("  - ms-sys is available on SourceForge or via yay on Arch")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Storage device detection
# ---------------------------------------------------------------------------

def _normalize(node):
    """Make every lsblk node uniform: absolute path, int size, mountpoint list."""
    node["path"] = node.get("path") or "/dev/" + node["name"]
    try:
        node["size"] = int(node.get("size") or 0)
    except (TypeError, ValueError):
        node["size"] = 0

    # Newer lsblk returns a list in "mountpoints", older ones a string in
    # "mountpoint". Merge both into a clean list without null entries.
    mounts = node.get("mountpoints")
    if mounts is None:
        mounts = [node.get("mountpoint")]
    node["mounts"] = [m for m in mounts if m]

    node["children"] = [_normalize(c) for c in node.get("children", [])]
    return node


def _lsblk_json():
    """Run lsblk in JSON mode, trying progressively simpler column sets."""
    last_error = ""
    for columns in LSBLK_COLUMN_SETS:
        result = subprocess.run(
            ["lsblk", "-J", "-b", "-o", columns],
            text=True, capture_output=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        last_error = result.stderr.strip()
    raise StepError("lsblk could not list devices: " + last_error)


def _sysfs_devices():
    """Fallback detection through /sys/block when lsblk is unavailable."""
    devices = []
    for name in sorted(os.listdir("/sys/block")):
        # Skip virtual devices: loop, ram, zram, device-mapper
        if name.startswith(("loop", "ram", "zram", "dm-", "sr", "md")):
            continue
        base = os.path.join("/sys/block", name)

        def read(rel):
            try:
                with open(os.path.join(base, rel)) as fh:
                    return fh.read().strip()
            except OSError:
                return ""

        sectors = int(read("size") or 0)
        # A USB disk exposes "usb" somewhere in its resolved sysfs path
        real = os.path.realpath(base)
        devices.append({
            "name": name,
            "path": "/dev/" + name,
            "size": sectors * 512,
            "model": read("device/model"),
            "tran": "usb" if "/usb" in real else "",
            "rm": read("removable") or "0",
            "type": "disk",
            "children": [],
        })
    return devices


def list_devices():
    """Return all whole-disk devices currently connected to the machine."""
    if shutil.which("lsblk"):
        data = _lsblk_json()
        disks = [d for d in data["blockdevices"] if d.get("type") == "disk"]
    else:
        disks = _sysfs_devices()
    # Hide virtual devices that can never be a USB target
    disks = [d for d in disks if not d["name"].startswith(("zram", "ram", "loop"))]
    return [_normalize(d) for d in disks]


def all_mounts(dev):
    """Return every mount point used by a disk and its partitions."""
    mounts = list(dev["mounts"])
    for child in dev["children"]:
        mounts.extend(all_mounts(child))
    return mounts


def is_usb_candidate(dev):
    """Treat a disk as a safe target if it is USB-attached or removable."""
    removable = str(dev.get("rm")).lower() in ("1", "true")
    return dev.get("tran") == "usb" or removable


def holds_system(dev):
    """True if any partition of the disk is mounted at a system location."""
    return any(m in PROTECTED_MOUNTS for m in all_mounts(dev))


def human_size(num_bytes):
    """Convert a byte count to a readable string."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def print_devices(devices):
    """Print a numbered table of devices with their partitions."""
    for i, dev in enumerate(devices, 1):
        model = (dev.get("model") or "unknown").strip()
        kind = "USB" if dev.get("tran") == "usb" else (
            "removable" if is_usb_candidate(dev) else "internal")
        flag = "  [HOLDS SYSTEM - blocked]" if holds_system(dev) else ""
        print(f"  {i}) {dev['path']}  {human_size(dev['size'])}  "
              f"{model}  ({kind}){flag}")
        for part in dev["children"]:
            fstype = part.get("fstype") or "-"
            label = part.get("label") or "-"
            mount = ", ".join(part["mounts"]) or "not mounted"
            print(f"       {part['path']}  {human_size(part['size'])}  "
                  f"fs={fstype}  label={label}  mount={mount}")


def get_candidates(show_all):
    """Return the devices that may be chosen as a target."""
    devices = list_devices()
    if not show_all:
        devices = [d for d in devices if is_usb_candidate(d)]
    return devices


def choose_device(show_all):
    """Show detected devices and let the user pick one."""
    candidates = get_candidates(show_all)
    if not candidates:
        sys.exit("No USB/removable storage devices detected. Plug in a drive "
                 "and try again (use --all to also show internal disks).")
    print("\nDetected storage devices:")
    print_devices(candidates)
    while True:
        answer = input("\nSelect device number (q to quit): ").strip().lower()
        if answer == "q":
            sys.exit(0)
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            dev = candidates[int(answer) - 1]
            if holds_system(dev):
                print("That disk holds the running system. Choose another.")
                continue
            return dev
        print("Invalid selection.")


def confirm_erase(dev):
    """Require the user to type the device path to confirm destruction of data."""
    print(f"\nALL DATA ON {dev['path']} WILL BE DESTROYED.")
    answer = input(f"Type the device path ({dev['path']}) to confirm: ").strip()
    if answer != dev["path"]:
        sys.exit("Confirmation did not match. Aborting.")


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def first_partition(device_path):
    """Return the path of the first partition for a device (handles nvme/mmc)."""
    if device_path[-1].isdigit():
        return device_path + "p1"
    return device_path + "1"


def unmount_all(device_path, dry_run):
    """Unmount every mounted partition that belongs to the device."""
    log(f"Unmounting all partitions of {device_path}")
    # -p prints full device paths, -l avoids the tree drawing characters
    out = subprocess.run(
        ["lsblk", "-lnp", "-o", "NAME,MOUNTPOINT", device_path],
        text=True, capture_output=True,
    ).stdout
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            run(["umount", parts[0]], dry_run, check=False)


def partition_and_format(device_path, label, dry_run):
    """Wipe the device, create one bootable NTFS partition and format it."""
    log("Creating MBR partition table with one bootable NTFS partition")
    # Remove old filesystem signatures so stale metadata does not interfere
    run(["wipefs", "-a", device_path], dry_run)

    # sfdisk script: DOS label, one partition using all space,
    # type 7 (HPFS/NTFS/exFAT), bootable flag set (*)
    script = "label: dos\n,,7,*\n"
    print("    $ sfdisk " + device_path + "   (script: label: dos / ,,7,*)")
    if not dry_run:
        result = subprocess.run(
            ["sfdisk", device_path], input=script, text=True, capture_output=True
        )
        if result.returncode != 0:
            raise StepError("sfdisk failed:\n" + result.stderr.strip())
        # Ask the kernel to re-read the partition table and wait for udev
        subprocess.run(["partprobe", device_path], capture_output=True)
        subprocess.run(["udevadm", "settle"], capture_output=True)
        time.sleep(2)

    part = first_partition(device_path)
    log(f"Formatting {part} as NTFS (label: {label})")
    run(["mkfs.ntfs", "-f", "-L", label, part], dry_run)
    return part


def write_boot_records(device_path, part_path, dry_run):
    """Write the Windows 7+ MBR to the disk and the NTFS PBR to the partition."""
    log("Writing MBR to the whole disk")
    run(["ms-sys", "-7", device_path], dry_run)
    log("Writing NTFS boot record (PBR) to the first partition")
    run(["ms-sys", "-n", "-p", part_path], dry_run)


def count_files(root):
    """Count files under a directory, used for progress reporting."""
    return sum(len(files) for _, _, files in os.walk(root))


def copy_tree_with_progress(src, dst):
    """Copy a directory tree file by file, printing a simple progress line."""
    total = count_files(src)
    done = 0
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in filenames:
            # copyfile avoids copying permission bits, which NTFS does not need
            shutil.copyfile(os.path.join(dirpath, name),
                            os.path.join(target_dir, name))
            done += 1
            percent = (done * 100) // max(total, 1)
            print(f"\r    Copying files: {done}/{total} ({percent}%)",
                  end="", flush=True)
    print()


def copy_installer(iso_path, part_path, dry_run):
    """Mount the ISO and the USB partition, copy files, then unmount both."""
    log("Mounting ISO and USB partition")
    if dry_run:
        run(["mount", "-o", "loop,ro", iso_path, "<iso_mountpoint>"], True)
        run(["mount", part_path, "<usb_mountpoint>"], True)
        print("    (files would be copied here)")
        run(["sync"], True)
        return

    iso_mnt = tempfile.mkdtemp(prefix="winusb_iso_")
    usb_mnt = tempfile.mkdtemp(prefix="winusb_usb_")
    iso_mounted = usb_mounted = False
    try:
        run(["mount", "-o", "loop,ro", iso_path, iso_mnt])
        iso_mounted = True
        run(["mount", part_path, usb_mnt])
        usb_mounted = True

        log("Copying Windows installation files (this may take a while)")
        copy_tree_with_progress(iso_mnt, usb_mnt)

        log("Flushing write buffers to the device")
        run(["sync"])
    finally:
        # Always try to unmount and remove temporary directories
        if usb_mounted:
            run(["umount", usb_mnt], check=False)
        if iso_mounted:
            run(["umount", iso_mnt], check=False)
        for path in (iso_mnt, usb_mnt):
            try:
                os.rmdir(path)
            except OSError:
                pass


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Create a bootable Windows USB drive from an ISO (Linux)."
    )
    parser.add_argument("iso", nargs="?",
                        help="Path to the Windows ISO image")
    parser.add_argument("-d", "--device",
                        help="Target device (e.g. /dev/sdb). If omitted, "
                             "you will be asked to choose one.")
    parser.add_argument("-l", "--label", default="WINUSB",
                        help="Filesystem label for the USB (default: WINUSB)")
    parser.add_argument("--list", action="store_true",
                        help="List detected storage devices and exit")
    parser.add_argument("-a", "--all", action="store_true",
                        help="Also show internal (non-USB) disks. System "
                             "disks stay blocked.")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Print the commands without executing them")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt (dangerous)")
    return parser.parse_args()


def main():
    args = parse_args()

    # Device listing mode: no root and no ISO needed
    if args.list:
        devices = get_candidates(args.all)
        if not devices:
            sys.exit("No storage devices detected.")
        print("Detected storage devices:")
        print_devices(devices)
        return

    if not args.iso:
        sys.exit("Missing ISO path. Usage: winusb.py windows.iso "
                 "[-d /dev/sdX]  (or use --list)")
    if not os.path.isfile(args.iso):
        sys.exit(f"ISO file not found: {args.iso}")
    if not args.dry_run:
        check_environment()

    # Step 1: detect and identify the target device
    if args.device:
        match = [d for d in list_devices() if d["path"] == args.device]
        if not match:
            sys.exit(f"{args.device} is not a detected whole-disk device. "
                     "Run with --list to see available devices.")
        dev = match[0]
        if holds_system(dev):
            sys.exit(f"{args.device} holds the running system. Refusing.")
        if not is_usb_candidate(dev) and not args.all:
            sys.exit(f"{args.device} does not look like a USB/removable "
                     "device. Use --all to override.")
    else:
        dev = choose_device(args.all)

    if not args.yes and not args.dry_run:
        confirm_erase(dev)

    device_path = dev["path"]

    try:
        # Step 1: unmount
        unmount_all(device_path, args.dry_run)
        # Step 2: partition and format
        part_path = partition_and_format(device_path, args.label, args.dry_run)
        # Step 3: boot records
        write_boot_records(device_path, part_path, args.dry_run)
        # Step 4: copy installer files
        copy_installer(args.iso, part_path, args.dry_run)
    except StepError as exc:
        sys.exit(f"\nError: {exc}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted by user. The USB may be in an inconsistent state.")

    # Step 5: final instructions
    print("\nDone. Remove the USB, boot the target PC, open the BIOS/UEFI "
          "boot menu and select the USB drive.")
    print("Note: an NTFS partition boots in Legacy/CSM mode. For pure UEFI "
          "boot, the firmware must support NTFS or you need a FAT32 EFI "
          "partition.")


if __name__ == "__main__":
    main()
