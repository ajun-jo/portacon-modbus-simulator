# PortaCON Modbus Simulator

Simulates a Porta Studio project's Modbus RTU/TCP devices on a PortaCON, so ptcsw can be tested without real
hardware. We're currently using the CA111111 PortaCON at our office.

## Setup

On your laptop (macOS, Linux or WSL; Claude Code needs a paid Claude plan):

```bash
curl -fsSL https://claude.ai/install.sh | bash      # install the Claude Code CLI
ssh-copy-id portacon@<ip>
git clone https://github.com/ajun-jo/portacon-modbus-simulator.git
cd portacon-modbus-simulator
./remote.sh portacon@<ip> install                  # copies it to the PortaCON, restarts it after reboots
claude
```

Then tell Claude the PortaCON's IP, the COM port and slave ID, and for each DP:

- register type
- address
- data type
- report interval
- function code for read, and for write if using both

In Porta Studio, set **프로필 → COM1 설정 → 포트 경로** to `/home/portacon/modbus-plc-simulator/ttyCOM1`
(likewise `ttyCOM2`, `ttyCOM3`; no spaces), then build and deploy.

Watch it at `http://<ip>:8080`. To update, `git pull` and run the install line again.
