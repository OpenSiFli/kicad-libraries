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

- GPIO 引脚按端口前缀分组（PA → "PA" unit，PB → "PB" unit）
- 非 GPIO 引脚（电源、模拟等）归入 "SYS" unit
- SYS unit 始终排在第一个（`units.insert(0, ...)`）
- 每个 unit 最多 64 引脚，超出则拆分（PA1, PA2...）

### 引脚布局

- GPIO unit 使用 pair_mode：按序号排列，前半右侧、后半左侧
- SYS unit 使用 type 分区：input/power_input 在左，output/power_output 在右
- 引脚间距 2.54mm（100mil），引脚长度 2.54mm
- 标签宽度自动计算，body 宽度自适应

### 符号继承（extends）

- 同一 series 中引脚定义相同的 variant 共享 `pin_group_id`（通过 YAML list 的 `id()` 判断）
- 第一个 variant 生成完整符号，后续 variant 使用 `extends` 引用，只覆盖属性

### SYS 模板系统

- 模板文件：`templates/sys/<model_id>.kicad_sym`
- 用途：为 SYS unit 提供手工调整的引脚布局（电源引脚位置、隐藏引脚等）
- 加载流程：检查模板 → 解析为 `SysTemplate` → 按 unit name 匹配 → 复制引脚位置和图形
- 自动导出：若无模板，生成默认布局后自动导出模板文件供后续手工编辑
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

从 `./SiliconSchema/footprint/*.yml`（若不存在则 `./footprint/*.yml`）加载封装定义。每个 YAML 文件包含：

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
