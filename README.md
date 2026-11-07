python3 winusb.py --list              list usb disk
python3 winusb.py --list --all        # List all disk
sudo python3 winusb.py windows.iso    # select USB
sudo python3 winusb.py windows.iso -d /dev/sdb -l WIN
python3 winusb.py windows.iso -d /dev/sdb -n   # dry-run
