# SiliconSchema 数据格式

## series.yaml 结构（build 产物）

```yaml
schema_version: 0.0.1
model_id: SF32LB52x
lifecycle: production
docs:
  - datasheet: {en: "url", zh: "url"}
pads:
  PA00: &PA00
    type: bidirectional
    functions:
      - GPIO_A0
      - I2C1_SDA
  VBAT: &VBAT
    type: power_input
    description: "Battery input"
variants:
  - part_number: SF32LB52BU36
    package: QFN-68-1EP_7x7mm_P0.35mm_EP5.49x5.49mm
    pins:
      - {number: "1", pad: *PA00}
      - {number: "2", pad: *VBAT}
```

## chip.yaml 与 series.yaml 的区别

- `chip.yaml`（源文件）：pad 只声明 `{type: bidirectional}`，pinmux 通过 `shared_pinmux` 字段引用 `common/pinmux/<family>/` 下的共享配置
- `series.yaml`（构建产物）：`functions` 已合并到每个 pad 中，是完整的自包含数据

## SiliconSchema 仓库布局

```text
SiliconSchema/
├── chips/<series>/chip.yaml       # 源文件（不含 pinmux）
├── common/pinmux/<family>/        # 共享 pinmux 配置
│   ├── pinmux.yaml
│   └── pinr.yaml
├── common/schema/                 # JSON Schema 校验
├── out/<series>/series.yaml       # build 产物（完整数据，应作为输入）
└── src/                           # 构建工具
```

## 数据模型映射（schema_loader.py）

| YAML 字段 | Python 类 | 说明 |
| --------- | --------- | ---- |
| 顶层 | `ChipSeries` | model_id, lifecycle, pads, variants |
| `pads.*` | `ChipPad` | name, type, pinmux |
| `pads.*.functions[]` | `PinmuxEntry` | function, description |
| `variants[]` | `ChipVariant` | part_number, package, pins, pin_group_id |
| `variants[].pins[]` | `ChipVariantPin` | number, pads（支持单个字符串或数组） |
