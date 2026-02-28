# 模块详解

## cli.py — 命令行入口

入口函数 `main()`。解析参数后构造 `GeneratorOptions`，调用 `runner.run()`。

CLI 默认按仓库固定布局自动定位子模块路径（`./SiliconSchema`、`./kicad-footprint-generator`、`./kicad-library-utils`）。

## runner.py — 编排逻辑

`run(options)` 是核心流程：

1. `SiliconSchemaRepository` 加载 series.yaml
2. 应用 series/variant 过滤器
3. 若需要封装：`FootprintGenerator.generate()` → `FootprintGenerationResult`
4. 若需要符号：`SymbolGenerator.generate(series, footprints)`
5. 符号生成依赖封装的 manifest（用于关联 footprint reference）

## schema_loader.py — 数据加载

`SiliconSchemaRepository(root)` 扫描 `root/out/*/series.yaml`（build 产物）。若未找到，会回退到旧布局 `root/chips/*/series.yaml`（兼容旧版本）。

YAML 中 pad 引用使用 YAML anchor/alias（`&PA00` / `*PA00`），解析时通过 `id()` 追踪 Python 对象身份来还原 pad 名称。这是关键实现细节——`pad_lookup` 用 `id(value)` 做 key。

## symbols.py — 符号生成（核心）

最复杂的模块（~720 行）。

### 引脚分组（Unit）

- GPIO 引脚按端口前缀分组（PA → "PA" unit，PB → "PB" unit）；每个端口 unit 最多 64 引脚，超出则拆分（PA1, PA2...）
- 非 GPIO 引脚（电源、模拟等）归入 "SYS" 系列 unit
  - 若 SYS 引脚数 ≤ 40：单一 "SYS"
  - 若 SYS 引脚数 > 40：按 `subsystem` 优先级装箱生成 "SYS1", "SYS2"...（混装 unit ≤ 40，单子系统可 > 40 且独占，同一子系统不跨 unit）
  - 子系统优先级：power → analog → rf → crystal → audio → mipi → usb → strapping
- SYS units 始终排在端口 units 之前

### 引脚布局

- GPIO unit 使用 pair_mode：按序号排列，前半右侧、后半左侧
- SYS unit 使用 type 分区：input/power_input 在左，output/power_output 在右
- 引脚间距 2.54mm（100mil），引脚长度 2.54mm
- 标签宽度自动计算，body 宽度自适应

### 符号继承（extends）

- 同一 series 中引脚定义相同的 variant 共享 `pin_group_id`（通过 YAML list 的 `id()` 判断）
- 第一个 variant 生成完整符号，后续 variant 使用 `extends` 引用，只覆盖属性

### SYS 模板系统

- 模板文件：`templates/sys/<model_id>__<part_number>.kicad_sym`（每个符号变体/引脚组一份，`part_number` 取该引脚组的第一个 variant，即未设置 `extends` 的基符号）
- 用途：为 SYS unit 提供手工调整的引脚布局（电源引脚位置、隐藏引脚等）
- 加载流程：检查模板 → 解析为 `SysTemplate` → 按 unit name 匹配 → 复制引脚位置和图形
- 向后兼容：若一个 series 只有一个引脚组，且 `templates/sys/<model_id>__<part_number>.kicad_sym` 不存在，会回退尝试 `templates/sys/<model_id>.kicad_sym`
- 模板只读：生成器不会修改 `templates/sys/`；若模板缺失或不匹配（缺 unit / 缺 pins），会输出 warning 并在输出目录生成建议模板 `output_dir/template/<model_id>__<part_number>.kicad_sym`
- 模板中的引脚通过 `(name, number)` 元组匹配

### 电气类型映射（PIN_TYPE_MAP）

SiliconSchema pad type → KiCad pin electrical type，例：`bidirectional` → `bidirectional`，`power_input` → `power_in`

### Alternate Functions

每个引脚的 pinmux 条目转换为 KiCad 的 `AltFunction`，在符号编辑器中显示为引脚的可选功能。

## footprints.py — 封装生成

`FootprintGenerator` 协调封装生成：

1. 收集所有 variant 引用的 package 名称
2. 从 `FootprintLibrary` 查找对应的参数定义
3. `NoLeadGeneratorAdapter` 调用上游 QFN/DFN 生成器
4. 输出到 `build/footprints/<library>.pretty/`
5. 生成 `manifest.json` 记录封装元数据

`load_footprint_manifest()` 用于 `--symbols-only` 模式，从已有 manifest 恢复封装信息。

## footprint_loader.py — 封装参数

若存在，优先从 `./SiliconSchema/footprint/*.yml`（否则 `./footprint/*.yml`）加载封装定义；若两者都不存在，则封装生成回退到 `kicad-footprint-generator/data/` 内置的 package specs。每个 YAML 文件包含：

- `defaults`：共享默认参数
- `packages[]`：具体封装，参数与 defaults 合并
- `file_header`：传递给上游生成器的头信息（含 library 名）

## upstream.py — 子模块集成

通过 `sys.path.insert()` 使子模块代码可被 import：

- `kicad-footprint-generator/src/` → 封装生成器
- `kicad-library-utils/common/` → `kicad_sym` 符号库操作

## 子模块依赖

| 子模块 | 用途 | 使用的关键组件 |
| ------ | ---- | ------------- |
| SiliconSchema | 芯片数据源 | `out/*/series.yaml`（build 产物，唯一真源） |
| kicad-footprint-generator | 封装生成 | `scripts/Packages/no_lead/ipc_noLead_generator` |
| kicad-library-utils | 符号操作 | `common/kicad_sym.py`（KicadLibrary, KicadSymbol, Pin, Rectangle, AltFunction） |

## 输出结构

```text
build/
├── footprints/
│   ├── <library>.pretty/
│   │   └── <package>.kicad_mod
│   └── manifest.json
└── symbols/
    ├── libs/
    │   └── MCU_SiFli.kicad_sym
    ├── metadata/
    │   └── <model_id>.json
    └── manifest.json
```
