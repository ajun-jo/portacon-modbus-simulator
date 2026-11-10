# PortaCON Modbus Simulator

Simulates a Porta Studio project's Modbus RTU/TCP devices on a PortaCON, so ptcsw can be tested without real
hardware. We're currently using the CA111111 PortaCON at our office.

## Setup

On the PortaCON (`ssh portacon@<ip>`):

```bash
git clone https://github.com/ajun-jo/portacon-modbus-simulator.git ~/modbus-plc-simulator
cd ~/modbus-plc-simulator
./run.sh ui
(crontab -l; echo '@reboot sleep 10; cd $HOME/modbus-plc-simulator && ./run.sh boot') | crontab -
python3 studio_to_init.py "project.json" --fresh
./run.sh reset com1 com2 com3
```

Crontab restarts it after reboots; `git pull` updates it.

In Porta Studio, set **프로필 → COM1 설정 → 포트 경로** to `/home/portacon/modbus-plc-simulator/ttyCOM1`
(likewise `ttyCOM2`, `ttyCOM3`; no spaces), then build and deploy.

## Configure DPs with Claude Code

On your laptop (needs a paid Claude plan), clone the repo and start Claude Code in it:

```bash
curl -fsSL https://claude.ai/install.sh | bash   # Windows: irm https://claude.ai/install.ps1 | iex
ssh-copy-id portacon@<ip>
git clone https://github.com/ajun-jo/portacon-modbus-simulator.git
cd portacon-modbus-simulator && claude
```

Tell Claude the PortaCON's IP, the COM port and slave ID, and for each DP:

- register type
- address
- data type
- report interval
- function code for read, and for write if using both
