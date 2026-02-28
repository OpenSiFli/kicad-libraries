# SiFli KiCAD Generator

English | [中文](README-zh.md)

用于从 SiliconSchema 构建产物自动生成 SiFli SoC/Module 的 KiCad symbols 与 footprints，并通过 GitHub Release 发布可直接在 KiCad PCM 中安装的 ZIP 包。

## 安装与使用（用户侧）

1. 从 [Releases](https://github.com/OpenSiFli/kicad-libraries/releases) 下载最新 `sifli-kicad-libraries-<tag>.zip`。
2. 打开 KiCad → **Tools** → **Package and Content Manager**。
3. 选择 **Install from file...** 并选中该 ZIP。

> 用户安装入口保持为 Release ZIP + PCM Install from file。

## 开发依赖

- Python >= 3.13
- [uv](https://docs.astral.sh/uv/)
- 已初始化 submodules（`SiliconSchema`、`kicad-footprint-generator`、`kicad-library-utils`）

## 本地生成流程

```bash
uv sync --frozen
uv run --directory SiliconSchema build-schema
uv run kicad-generator --output-dir build
```

关键顺序：**先构建 SiliconSchema，再运行生成器**。

## 自动发布流程（Tag 驱动）

`/.github/workflows/release.yml` 在 tag push 时自动执行：

1. `actions/checkout` + `submodules: recursive`
2. 安装 `uv`
3. `uv sync --frozen`
4. `uv run --directory SiliconSchema build-schema`
5. `uv run kicad-generator --output-dir build`
6. 将生成结果整理为发布结构（`symbols/`、`footprints/`、`resources/`、`metadata.json`）
7. `uv run scripts/build_release.py --source-dir release-staging --tag <tag>`
8. 通过 `ncipollo/release-action` 上传 ZIP 和 `metadata-upstream.json`

## 贡献方式

推荐贡献入口：

- schema/module/template 变更（`SiliconSchema` / `modules` / `templates`）
- 生成器逻辑变更（`src/kicad_generator`）

发布产物不再以手工维护为主，而是由 CI 自动生成并随 tag 发布。
