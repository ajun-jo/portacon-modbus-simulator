# PortaCON Modbus Simulator

Simulates a Porta Studio project's **Modbus RTU/TCP** devices on a PortaCON, so ptcsw can be tested without real
hardware. We're currently using the CA111111 PortaCON at our office.

## 1. Setup (once, on your laptop)

macOS, Linux or WSL. Claude Code needs a paid Claude plan.

```bash
curl -fsSL https://claude.ai/install.sh | bash   # install Claude Code
ssh-copy-id portacon@<ip>
git clone https://github.com/ajun-jo/portacon-modbus-simulator.git
cd portacon-modbus-simulator
./remote.sh portacon@<ip> install
claude
```

## 2. Tell Claude what to simulate

Fill this in and paste it, **one line per DP**:

```text
Example:

PortaCON <ip>, COM1 (or Modbus TCP), slave 1.
dp160: input register 200, FLOAT32, 1000 ms, read FC04, send 0.125
dp163: holding register 220, INT16, 1000 ms, read FC03 / write FC06, send 12345
```

Claude sets up the simulator and tells you what to enter in Porta Studio.

## 3. How to Set up Porta Studio

- **RTU:** 프로필 → COM1 설정 → 포트 경로 = `/home/portacon/modbus-plc-simulator/ttyCOM1`
  > ⚠️ No spaces. Check it after every deploy.
- **TCP:** device IP `<ip>`, port from Claude.
- **Data types must match** what you told Claude.

## 4. Check

Build and deploy, then ask Claude to **"check the values"**. Watch it live at `http://<ip>:8080`.

To update: `git pull`, then run the `install` line again.
