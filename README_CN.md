# TCA9548A Klipper Add-on

[English](README.md) | [简体中文](README_CN.md)

用于 Klipper 的 TCA9548A I2C 复用器扩展。它管理通道选择和复用器后设备的串行
访问，并提供 AHT1x、AHT2x、AHT3x、BME280 和 SHT3X 的 Klipper 温度传感器适配器。

项目最初面向 [EMU](https://github.com/DW-Tas/EMU) 部署场景：MMB/AFC 控制板的
I2C 接口不足时，TCA9548A 可让多个下游设备共用一条硬件 I2C 总线，同时按复用器
通道独立寻址。

每次受支持环境传感器的初始化或测量只会在完整操作期间选中对应通道，完成后关闭
TCA9548A 的全部通道。这样空闲或断开的下游支路会与共享的上游 I2C 总线隔离。

## 目录

- [硬件与接线](#hardware)
  - [可选硬件复位](#hardware-reset)
- [安装](#installation)
  - [I2C 恢复特征检查](#i2c-recovery-check)
- [维护](#maintenance)
  - [Fluidd/Mainsail 更新](#fluidd-mainsail-updates)
- [配置](#configuration)
- [运行](#operation)
  - [运行期命令](#runtime-commands)
  - [环境传感器恢复](#environment-recovery)
- [调试](#debugging)
- [范围与限制](#scope-and-limits)
- [许可证](#license)

<a id="hardware"></a>
## 硬件与接线

<p align="center">
  <img src="images/TCA9548A.jpg" alt="TCA9548A HW-617 转接板" width="50%">
</p>

上图为常见的 HW-617 TCA9548A 转接板。将上游总线接至 MCU 或控制板：

| TCA9548A 引脚 | 连接方式 |
| --- | --- |
| `VIN` | 3.3V 或 5V 电源 |
| `GND` | 控制器地 |
| `SDA` | 控制器 I2C SDA |
| `SCL` | 控制器 I2C SCL |
| `RST` | 通过电阻上拉至 `VIN`/VCC，不能悬空。可选的开漏 MCU GPIO 可将其拉低以复位。 |
| `A0`、`A1`、`A2` | 保持未连接时使用默认 I2C 地址 `0x70`（十进制 `112`） |

将每个下游设备的 SDA/SCL 接到相应的复用器通道：通道 0 使用 `SD0`/`SC0`，通道 7
使用 `SD7`/`SC7`。下游设备的供电和地线需要分别连接。相同 I2C 地址的设备可放在
不同通道，因为同一时间只有被选中的通道会与上游总线连通。

<a id="hardware-reset"></a>
### 可选硬件复位

在复用器段设置 `reset_pin` 后，扩展可以不经 I2C 直接复位 TCA9548A。
`reset_active_high` 表示“请求复位的 MCU GPIO 电平”。不要在 `reset_pin` 上使用
Klipper 的 `!` 反相前缀；扩展会拒绝这种写法，以确保极性只有一个明确的配置来源。
设置了 `reset_pin` 但没有设置此选项时，默认值为 `True`，匹配本板的 N-MOS 下拉复位
电路。

| 硬件连接 | `reset_active_high` | MCU 正常输出 | MCU 复位输出 |
| --- | --- | --- | --- |
| GPIO 直接连接 TCA `RESET#` | `False` | 高 | 低 |
| GPIO 接 N-MOS 栅极，N-MOS 将 `RESET#` 拉低 | `True` | 低 | 高 |

当 TCA9548A 供电为 5V 时，使用 N-MOS 下拉方案。`RESET#` 通过 10k 电阻上拉至 TCA
的 5V 电源；MOS 漏极接 `RESET#`、源极接 GND、栅极接 MCU GPIO，并用 10k 栅极下拉
电阻接 GND。这样 5V 上拉能够提供有效高电平，而 MCU 只驱动 MOS 栅极。3.3V 推挽
GPIO 直接接到 5V 的 `RESET#` 无法将其释放到 5V，不建议这样连接。

本板 N-MOS 电路应设置 `reset_pin: EMU_1:PC12` 与
`reset_active_high: True`；完整示例见[配置](#configuration)。

在 Klipper 启动、MCU 重启和 Klipper 关闭时，该引脚都会被设为正常的非复位电平。一次
复位脉冲会清除 TCA9548A 控制寄存器并断开所有下游 I2C 通道；它不会切断下游设备的
3.3V/5V 电源。

Klipper 启动后，请按[运行期命令](#runtime-commands)中的复位顺序验证接线。

#### 自动复用器恢复

自动恢复需要配置 `reset_pin`，并使用新版 I2C 协议。

- **触发条件：** TCA 控制寄存器传输返回 I2C 错误，或主机未收到 `i2c_response`
  （`NO_RESPONSE`）。复位验证成功后，会将原 TCA 操作重试一次。
- **不会直接复位传感器：** 下游传感器的 `START_NACK` 等错误仍是传感器错误；只有它之后
  阻碍访问 TCA 控制寄存器时，才会触发复用器恢复。
- **恢复失败：** 复位验证失败，或验证成功后的单次重试仍失败时，TCA 与下游 I2C 都会
  暂停一个 `environment_report_time` 周期。本轮不再复位或重试，同一复用器的其他传感器
  也不能立即触发新的恢复。
- **停止条件：** 连续 5 次自动恢复失败后（复位验证失败，或复位验证成功但紧随其后的
  一次重新选通失败），所有自动 TCA 和下游 I2C 操作都会停止。
  排除故障后执行 `TCA_RESET MUX=mux1`；若仍无法验证复位，请给打印机重新上电。
  完整的 TCA 控制操作成功才会清除失败计数，自动复位验证成功本身不会清零。
  手动 `TCA_RESET` 验证成功也会清零计数，并重新启用自动恢复。

默认每 30 秒最多自动尝试一次。控制台会显示如
`TCA9548A mux1: BUS_TIMEOUT; hardware reset` 的错误行；恢复成功后显示
`reset verified; retrying`。验证失败只表示 GPIO 脉冲已请求、I2C 未能确认结果，不代表
GPIO 脉冲本身失败。复用器状态包含复位配置、计数、结果与恢复暂停状态。

<a id="installation"></a>
## 安装

在 Klipper 或 Kalico 主机上运行：

```bash
cd ~
git clone https://github.com/jacksky6/TCA9548A-klipper-addon.git
cd TCA9548A-klipper-addon
./install.sh
```

安装程序会显示当前 Git 分支，以 10 秒超时检查该分支远端是否有更新，并在更新前询问。
之后它会检测 Klipper 或 Kalico，并在其 extras 目录创建以下软链接：

```text
tca9548a.py
tca9548a_drivers/
```

为兼容升级，安装程序会移除这两个准确路径上原有的软链接、普通文件或目录，然后创建
当前软链接。因此旧版单文件安装和旧版驱动目录会自动迁移。

安装程序不会重启 Klipper 或 Kalico；安装后请在 Fluidd/Mainsail 中执行“重启 Klipper”。

单独使用 `-b` 或 `--branch` 会列出本地和 `origin` 分支，然后交互选择。使用
`-b BRANCH` 或 `--branch BRANCH` 可直接切换，例如 `./install.sh -b dev`。请求的
分支不在本地时，安装程序会以同样的 10 秒超时从 `origin` 获取。使用 `-s` 或
`--skip-update` 可跳过远端更新检查并从当前本地文件安装；同时使用 `-b BRANCH` 时，
目标分支必须已存在于本地或本地 `origin` 缓存中。非交互运行时，发现更新只会提示，
安装仍使用本地文件继续执行。

<a id="i2c-recovery-check"></a>
### I2C 恢复特征检查

安装时，脚本会检查目标 Klipper 或 Kalico 的 `bus.py` 是否具备返回
`i2c_bus_status` 的现代 `i2c_transfer` 协议。它会显示检测到的固件类型和当前 Git
版本。该协议由 Klipper 提交 `8965958a8b6c` 于 2026-02-07 引入，`git describe` 显示
为 `v0.13.0-525-g8965958`；需要此版本或更新的、相互匹配的主机和 MCU 固件。检测结果
分为两个层级：

- **支持的主机源码：** 扩展的可恢复环境传感器驱动可在主机侧接收 I2C `NACK`、`START_NACK`、
  `START_READ_NACK` 与 `BUS_TIMEOUT` 状态，而不经过会将这些状态升级为主机停机的
  Klipper 公共 I2C 辅助接口。未收到 `i2c_response` 的查询也会由扩展拦截并显示为
  `NO_RESPONSE`；它不是 MCU 返回的 I2C 总线状态。相关 MCU 仍必须用匹配的现代固件
  重新编译并刷写。驱动的 `i2c_status_supported` 状态字段显示配置 MCU 的最终运行时结果。
- **旧版主机源码：** 旧版 MCU 固件会在 Python 驱动处理前，对这类 I2C 失败执行自身的
  `shutdown()`。扩展仍兼容此类源码，但无法保证缺失或失效的环境传感器不会停止
  Klipper/Kalico。安装程序会解释该限制并要求确认后才继续。

有意在旧版目标上非交互安装时，使用：

```bash
./install.sh --allow-legacy-i2c
```

该参数只表示接受限制，不会让旧版主机或 MCU 固件获得恢复能力。

<a id="maintenance"></a>
## 维护

### 卸载

用以下任一命令移除已安装的软链接：

```bash
./install.sh --uninstall
# 或：./install.sh -u
```

仓库目录会保留。若配置了 Moonraker 更新，请手动从 `moonraker.conf` 移除
`[update_manager tca9548a]` 段，重启 Moonraker，然后在 Fluidd/Mainsail 中重启 Klipper。

<a id="fluidd-mainsail-updates"></a>
### Fluidd/Mainsail 更新

Fluidd 和 Mainsail 通过 Moonraker Update Manager 显示更新状态。初始安装完成后，在
`~/printer_data/config/moonraker.conf` 中添加：

```ini
[update_manager tca9548a]
type: git_repo
path: ~/TCA9548A-klipper-addon
origin: https://github.com/jacksky6/TCA9548A-klipper-addon.git
primary_branch: main
install_script: install.sh
is_system_service: False
```

保存后重启 Moonraker。更新页将显示此仓库，并在每次更新后运行 `install.sh` 以保持
软链接正确。脚本会尝试快进 Git 更新，然后替换其两个扩展路径上的现有文件、目录或
软链接，再重新创建当前软链接。

旧版 I2C 目标上，Moonraker 会以非交互方式运行，安装将在恢复能力确认处停止。请升级
Klipper/Kalico 和 MCU 固件，或在 `moonraker.conf` 中明确接受限制：

```ini
install_script: install.sh --allow-legacy-i2c
```

此配置特意未设置 `managed_services`，所以更新不会重启 Klipper。
`is_system_service: False` 也告知 Moonraker `tca9548a` 不是可重启的系统服务。准备好
加载更新后的扩展时，请手动在 Fluidd/Mainsail 中重启 Klipper。

请在自己的 `printer.cfg` 中配置 `[tca9548a]` 和 `[temperature_sensor]` 段，使其符合
实际连接的传感器、I2C 地址和复用器通道。不要不加修改地复制示例配置。

<a id="configuration"></a>
## 配置

### 支持的传感器类型

当前版本仅支持以下温湿度传感器类型：

```text
AHT1X_TCA9548A
AHT2X_TCA9548A
AHT3X_TCA9548A
BME280_TCA9548A
SHT3X_TCA9548A
```

### 配置参考

以下参考涵盖多个支持的传感器类型。只保留与实际硬件相符的段落，并按硬件调整复用器
设置、通道号、I2C 地址和温度范围。

```ini
[tca9548a mux1]
i2c_mcu: EMU_1
i2c_bus: i2c1_PB6_PB7
i2c_address: 112 # 0x70，A0/A1/A2 均为低；0x71-0x77 使用 113-119
environment_report_time: 120
# 可选：打印中 AFC 或 Happy Hare 换料动作期间跳过环境传感器采样。
# Klipper ready 后延迟 30 秒检测一次。
# pause_env_on_toolchange: True
# 受支持环境传感器通信失败时，将相应值显示为 0，而非保留最后一次有效值。
# 只能在此复用器段设置，不能写入 [temperature_sensor] 段。
# zero_temperature_on_error: False
# zero_humidity_on_error: False
# 可选 TCA RST 控制，见“可选硬件复位”。
# reset_pin: EMU_1:PC12
# reset_active_high: True
# 板载 N-MOS 电路：PC12 为高时将 TCA RST 拉低；PC12 为低时正常工作。
# reset_pulse_time: 0.010          # 默认：10 ms
# reset_settle_time: 0.010         # 默认：10 ms
# reset_recovery_cooldown: 30      # 默认：30 s

[temperature_sensor Lane_0]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 0
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_1]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 1
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_2]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 2
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_3]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 3
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_4]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 4
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Chamber_BME]
sensor_type: BME280_TCA9548A
tca9548a: mux1
tca9548a_channel: 5
i2c_address: 118
min_temp: -20
max_temp: 80

[temperature_sensor Chamber_SHT]
sensor_type: SHT3X_TCA9548A
tca9548a: mux1
tca9548a_channel: 6
i2c_address: 68
min_temp: -20
max_temp: 80
```

`[tca9548a mux1]` 段同时定义复用器并加载插件，不需要独立的 `[tca9548a]` 段。将该段
放在使用 `*_TCA9548A` 传感器类型的所有 `[temperature_sensor]` 段之前。

在 `[tca9548a mux1]` 中填写控制板的 Klipper MCU 名称和 I2C 总线名：`i2c_mcu` 与
`i2c_bus`。该段是所有复用器后设备的 `i2c_mcu`、`i2c_bus`、`i2c_speed` 和软件 I2C
引脚设置的唯一来源；下游传感器段中同名的设置会被忽略。示例使用
`EMU_1` 与 `i2c1_PB6_PB7`。

`environment_report_time` 是该复用器下环境传感器的轮询间隔，单位秒，默认为 `120`。
TCA9548A AHT 传感器段刻意不支持 `aht10_report_time`；请在复用器段设置共享轮询间隔，
以便一起调度所有通道。`BME280_TCA9548A` 不支持单传感器 `bme280_report_time`，
`SHT3X_TCA9548A` 也使用复用器间隔，不支持单传感器 `sht3x_report_time`。

`pause_env_on_toolchange` 默认为 `False`。设为 `True` 后，扩展会在 Klipper
ready 30 秒后一次性检测已加载的 AFC 和 Happy Hare 对象，并将结果记录到
`klippy.log`。打印进行时，AFC 的 `current_state` 或 Happy Hare 的 `action`
只要不是 `Idle`，就会在选中 TCA 通道和提交 I2C 前跳过该次环境传感器采样。
多色系统处于 `Idle` 的正常打印，以及所有非打印操作，仍会正常采样。AFC 未提供
`current_state`、Happy Hare 未提供 `action`，或未检测到多色系统时，仍会正常采样。

`zero_temperature_on_error` 和 `zero_humidity_on_error` 是 AHT、BME280 和 SHT3X
通信失败时的可选布尔设置，默认均为 `False`，分别保留温度或湿度的最后一次有效值。将其中
任一项设为 `True`，则仅在失败后将对应值报告为 `0`。这两个选项只能写在
`[tca9548a ...]` 复用器段，不支持在单独的 `[temperature_sensor ...]` 段设置。由失败
产生的零值不会参与 `min_temp` 或 `max_temp` 检查。

Klipper 启动时，每个复用器会记录环境传感器调度计划。同一复用器下的传感器会在
`environment_report_time` 内均匀错开，避免周期轮询集中在同一时刻。

<a id="operation"></a>
## 运行

<a id="runtime-commands"></a>
### 运行期命令

所有命令使用 `[tca9548a <名称>]` 中配置的复用器名称。以下示例使用 `mux1`。

### 环境传感器采样

独立执行 purge 或其他希望暂缓环境传感器 I2C 通信的高负载操作前后，可执行：

```gcode
TCA_PAUSE_ENV_SAMPLING MUX=mux1
# 执行 purge 或其他高负载操作。
TCA_RESUME_ENV_SAMPLING MUX=mux1
```

`TCA_PAUSE_ENV_SAMPLING` 会为该复用器下所有受支持的 AHT、BME280 和 SHT3X
环境传感器设置手动暂停。它不会停止或重建计时器、改变当前 TCA 通道、复位复用器，
也不会阻止其他 I2C 设备。每次环境传感器原有的定时采样到达时，只会跳过本次 I2C
操作。

`TCA_RESUME_ENV_SAMPLING` 只清除此手动暂停。各传感器会在自己下一个原有且错开的
采样周期恢复，不会同时立即采样。若启用了 `pause_env_on_toolchange: True`，手动恢复后
正在进行的 AFC 或 Happy Hare 换料仍会继续暂停采样。

### 复用器控制与复位

在打印机空闲且预期没有下游设备通信时，可使用下列命令手动检查复用器：

```gcode
TCA_SELECT MUX=mux1 CHANNEL=0
TCA_STATUS MUX=mux1
TCA_SELECT MUX=mux1
```

`TCA_SELECT` 可选择 `0` 至 `7` 中的一个通道；省略 `CHANNEL` 会关闭全部复用器通道。
`TCA_STATUS` 会读取 TCA 控制寄存器，报告已选通通道，以及当前 I2C 接口是否支持新版的
状态响应。

配置了 `reset_pin` 时，可用下面的顺序测试硬件复位：

```gcode
TCA_SELECT MUX=mux1 CHANNEL=0
TCA_STATUS MUX=mux1
TCA_RESET MUX=mux1
TCA_STATUS MUX=mux1
```

先确认状态命令显示已选通的通道，再执行复位。在新版 I2C 固件上，`TCA_RESET` 只有在发送
复位脉冲后读回 TCA 控制寄存器为 `0x00` 时，才会报告 `reset verified`；最后的状态命令应显示
`active_channels=none`。这能证明选通的通道已被复位清除。在旧版 I2C 固件上，命令会显示
`pulsed (not verified on legacy I2C)`：GPIO 脉冲已发送，但失败的验证可能使 MCU 停机，
软件不能安全地证明复位结果。复位只断开下游 I2C 通道，不会切断下游设备供电。

<a id="environment-recovery"></a>
### 环境传感器 I2C 恢复

所有受支持传感器都使用 `tca9548a_drivers/` 中的独立驱动，由本插件负责初始化、采样和
恢复。它们共享同一复用器传输层，自动识别 Klipper/Kalico 的新版或旧版 I2C 接口。
不继承系统已安装的传感器驱动，也无需修改 Klipper/Kalico 本身。

#### 采样与重试

使用新版主机和匹配 MCU 固件时，初始化或采样失败会将该读数标为无效，并在下一个共享的
`environment_report_time` 重试完整初始化；重试成功后读数重新有效。

- 每个可恢复 I2C 操作只执行一次，并关闭主机层重试。
- AHT 与 SHT3X 每个计划周期只测量一次。AHT 返回 busy 或 SHT3X 获取失败时，本次直接
  结束，不重复命令、不发送软复位。
- 默认保留最后一次有效温湿度值。复用器级 `zero_temperature_on_error` 和
  `zero_humidity_on_error` 可分别改为报告零值。
- BME280 已关闭气压采样与补偿，因此对外温湿度字段与 AHT 一致。

#### 失败上限与报告

连续 15 次失败后，只有该传感器会在当前 Klipper 进程内停止采样，不再提交 I2C，也不会
影响同一复用器的其他传感器。排除故障后执行 `FIRMWARE_RESTART` 重新初始化。
`klippy.log` 会在第 5、10 次失败记录汇总，并在第 15 次记录最终停止。

相同错误在 `klippy.log` 中会被限频。Fluidd/Mainsail 控制台会将失败阶段、I2C 状态和
重试间隔显示为错误行，例如
`TCA9548A BME280 chamber: measurement failed: START_NACK; retry in 120s`。已显示失败后，
首次重试成功会显示 `recovered`。测量失败后若出现 `initialization failed`，表示驱动正在
再次采样前重新初始化。

Klipper 处于 shutdown 时，受支持传感器会立即停止且不再提交 I2C；待发送的控制台通知会被
抑制，日志只记录一次停止。每个传感器状态包含 `valid`、`communication_ok`、`last_error`、
`last_error_time`、`last_success_time`、`i2c_error_count`、`error_count`、
`consecutive_failure_count`、`sampling_stopped`、`i2c_status_supported` 和
`tca9548a_channel`。关联的 `temperature_sensor` 可用时也会包含这些字段。

#### 范围与地址

恢复机制只适用于受支持温湿度传感器，不改变其他下游 I2C 设备的错误语义。配置传感器的
`min_temp` 或 `max_temp` 违反仍属于 Klipper 安全停机。传感器 I2C 错误本身不会切换 `RST`、
复位下游设备或切断其供电；只有之后发生的 TCA 控制错误才可能使用前述可选复用器恢复。

若 A0/A1/A2 并非全部为低，请修改默认 `112`（`0x70`）的 `i2c_address`。Klipper 使用十进制
地址：AHT20 的 `0x38` 写为 `56`，默认 SHT3X 的 `0x44` 写为 `68`。

<a id="debugging"></a>
## 调试

仅测试复用器时，可启用：

```ini
[tca9548a mux1]
debug_no_disable: True
# select_delay: 0.01
# verify_select: True

[temperature_sensor Lane_0]
debug_skip_init: True
```

这样启动时不会自动向复用器写入数据，也不会初始化 AHT2X 传感器。Klipper 到达 ready
状态后，可使用上方“运行期命令”章节中的命令手动测试复用器。

`select_delay` 在每次复用器写入后等待，默认 `0`，因为 TCA9548A 通常不需要命令处理
延迟。`verify_select` 会在每次写入后读回控制寄存器，只有读回字节与请求通道掩码一致时
才报告成功。

`debug_no_disable` 默认 `False`。它为 `False` 时，插件会在 Klipper 启动期间向
TCA9548A 写入 `0x00`，在正常传感器初始化前关闭全部复用器通道。设为 `True` 会跳过
启动时的关闭写入，只适合调试复用器硬件状态。

`debug_skip_init` 默认 `False`。对于 AHT 复用器传感器，设为 `True` 会跳过传感器初始
化，使 Klipper 可以先进入 ready 状态后再手动测试复用器。正常配置应保持两个调试选项
未设置。

<a id="scope-and-limits"></a>
## 范围与限制

仅支持[配置](#configuration)中列出的五种温湿度传感器类型。它们都使用
`tca9548a_drivers/` 中的独立驱动及本扩展的复用器传输层。所有适配器都会在每次 I2C 操作前重新选择
TCA9548A 通道，避免在 reactor 暂停期间依赖之前的复用器状态。

<a id="license"></a>
## 许可证

本项目使用 GNU General Public License v3.0 或更高版本，详见 [LICENSE](LICENSE)。
