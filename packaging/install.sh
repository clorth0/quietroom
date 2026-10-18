#!/bin/bash
# Quietroom node install for Raspberry Pi OS / Debian. Run as root.
set -euo pipefail
apt-get update
apt-get install -y hackrf python3 python3-venv libportaudio2
id -u quietroom >/dev/null 2>&1 || useradd --system --home /var/lib/quietroom quietroom
install -d -o quietroom -g quietroom /var/lib/quietroom
install -d /etc/quietroom
[ -f /etc/quietroom/config.toml ] || install -m 0640 -g quietroom config.example.toml /etc/quietroom/config.toml
python3 -m venv /opt/quietroom-venv
/opt/quietroom-venv/bin/pip install --upgrade pip
/opt/quietroom-venv/bin/pip install .
ln -sf /opt/quietroom-venv/bin/quietroom-node /usr/local/bin/quietroom-node
install -m 0644 packaging/systemd/quietroom-node.service /etc/systemd/system/
install -m 0644 packaging/udev/99-quietroom-sdr.rules /etc/udev/rules.d/
udevadm control --reload-rules
systemctl daemon-reload
systemctl enable quietroom-node
echo "Installed. Edit /etc/quietroom/config.toml, then: systemctl start quietroom-node"
