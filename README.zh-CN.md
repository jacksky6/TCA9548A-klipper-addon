# TCA9548A Klipper 插件

[English](README.md)

这是一个用于 Klipper 的 TCA9548A I2C 多路复用器插件，面向 EMU/MMU 这类需要挂载多个温湿度传感器的场景。

插件尽量复用 Klipper 原生传感器驱动，只在每次 I2C 读写前自动切换 TCA9548A 通道，并把同一个 mux 下的环境传感器按统一周期错峰轮询。

## 安装

把 `tca9548a.py` 复制到 Klipper 的 `klippy/extras/` 目录：

```text
~/klipper/klippy/extras/tca9548a.py
```

然后把类似 `tca9548a_aht2x_example.cfg` 的配置加入 `printer.cfg`。

当前支持的传感器类型：

```text
AHT1X_TCA9548A
AHT2X_TCA9548A
AHT3X_TCA9548A
BME280_TCA9548A
SHT3X_TCA9548A
```

## 配置示例

```ini
[tca9548a mux1]
i2c_mcu: mmu
i2c_bus: i2c2_PB10_PB11
i2c_address: 112 # 0x70，A0/A1/A2 全部接低；0x71-0x77 对应 113-119
environment_report_time: 30
# debug_no_disable: True

[temperature_sensor Lane_0]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 0
i2c_address: 56
min_temp: -20
max_temp: 80
# debug_skip_init: True

[temperature_sensor Lane_1]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 1
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

`[tca9548a mux1]` 既定义 mux，也负责加载插件，不需要额外添加单独的 `[tca9548a]` 段。这个 mux 配置段要放在所有使用它的 `[temperature_sensor]` 段之前。

`i2c_mcu` 和 `i2c_bus` 使用 Klipper 中对应 MCU 和 I2C 总线的名字。挂在这个 mux 后面的传感器默认继承 mux 的 `i2c_mcu`、`i2c_bus`、`i2c_speed`，除非在传感器段里单独覆盖。

`environment_report_time` 是同一个 mux 下所有环境传感器的统一轮询周期，单位是秒，默认值为 `30`。插件不允许在单个传感器段里配置单独的轮询周期：

```text
aht10_report_time
bme280_report_time
sht3x_report_time
```

如果写了这些选项，Klipper 会直接报配置错误。这样做是为了让同一个 mux 下的所有温湿度传感器统一调度，避免采样扎堆导致 MCU 负载瞬间升高。

Klipper 启动时，每个 mux 会在日志里打印环境传感器调度计划。插件会把同一个 mux 下的传感器均匀分布到 `environment_report_time` 周期内。例如 5 个传感器、30 秒周期，大致会按 6 秒间隔错峰轮询。

I2C 地址在 Klipper 配置里使用十进制：

```text
TCA9548A 0x70 -> 112
AHT20    0x38 -> 56
BME280   0x76 -> 118
SHT3X    0x44 -> 68
```

如果 TCA9548A 的 A0/A1/A2 接法不同，需要把 mux 的 `i2c_address` 改成对应的 `112` 到 `119`。

## 调试

只测试 mux 时，可以开启：

```ini
[tca9548a mux1]
debug_no_disable: True
# select_delay: 0.01
# verify_select: True

[temperature_sensor Lane_0]
debug_skip_init: True
```

这样启动时不会自动关闭 mux，也不会初始化 AHT 传感器。Klipper ready 后可以手动测试：

```gcode
TCA_SELECT MUX=mux1 CHANNEL=0
TCA_STATUS MUX=mux1
TCA_SELECT MUX=mux1
```

第一条命令选择 channel 0。`TCA_STATUS` 会读取 TCA9548A 控制寄存器。最后一条命令不带 `CHANNEL`，用于关闭所有通道。

`select_delay` 会在每次写 mux 后等待一小段时间，默认是 `0`，正常 TCA9548A 不需要额外延时。`verify_select` 会在每次选择通道后读回控制寄存器确认是否成功，只建议调试时开启。

## 说明

这个插件目前只面向少量温湿度传感器场景。它不会复制 Klipper 原生传感器算法，而是继承原生驱动，并把 I2C 对象替换为 mux 包装后的 I2C 对象。

当前设计重点是降低多个环境传感器同时采样时的风险：同一个 TCA9548A 后面的传感器使用统一周期，并由插件在启动时自动错峰调度。
