# SiFli KiCAD 库

[English](README.md) | 中文

用于从 SiliconSchema 构建产物自动生成 SiFli SoC/Module 的 KiCad symbols 与 footprints，并通过 GitHub Release 发布可直接在 KiCad PCM 中安装的 ZIP 包。

## 📦 安装方法（用户侧）

1. 从 [Releases](https://github.com/OpenSiFli/kicad-libraries/releases) 下载最新 `sifli-kicad-libraries-<tag>.zip`。
2. 打开 KiCad → **工具** → **拓展内容管理器**。
3. 选择 **从文件安装...** 并选中 ZIP。

> 用户安装入口保持为 Release ZIP + PCM Install from file。

## 🔧 开发环境

- Python >= 3.13
- `uv`
- 已拉取 submodule：`SiliconSchema`、`kicad-footprint-generator`、`kicad-library-utils`

## 🏗️ 本地生成

```bash
uv sync --frozen
uv run --directory SiliconSchema build-schema
uv run kicad-generator --output-dir build
```

关键顺序：**必须先构建 SiliconSchema，再运行生成器**。

## 🚀 自动发布（Tag 驱动）

推送 tag 后，`/.github/workflows/release.yml` 自动执行：

1. checkout + recursive submodules
2. 安装 `uv`
3. `uv sync --frozen`
4. `uv run --directory SiliconSchema build-schema`
5. `uv run kicad-generator --output-dir build`
6. 整理发布目录（`symbols/`、`footprints/`、`resources/`、`metadata.json`）
7. `uv run scripts/build_release.py --source-dir release-staging --tag <tag>`
8. 上传 ZIP 与 `metadata-upstream.json` 到 GitHub Release

## 🤝 贡献

推荐贡献入口：

- schema / module / template（`SiliconSchema`、`modules`、`templates`）
- 生成器代码（`src/kicad_generator`）

发布产物以 CI 自动生成为主，不再依赖手工维护。
