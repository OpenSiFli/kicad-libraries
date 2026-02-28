# Maintainer Guide

本指南面向仓库维护者，说明生成与发布链路。用户安装方式请以 `README.md` / `README-zh.md` 为准。

## Prerequisites

- Python >= 3.13
- [uv](https://docs.astral.sh/uv/)
- submodules initialized (`SiliconSchema`, `kicad-footprint-generator`, `kicad-library-utils`)

## Local Generation Workflow

关键顺序：**先构建 SiliconSchema，再运行本仓库生成器**。

```bash
uv sync --frozen
uv run --directory SiliconSchema build-schema
uv run kicad-generator --output-dir build
```

## Release Packaging Workflow

发布结构由 CI staging 目录整理而来，包含：

- `symbols/MCU_SiFli.kicad_sym`
- `footprints/*.pretty/*.kicad_mod`
- `resources/*`
- `metadata.json`

打包脚本：

```bash
uv run scripts/build_release.py --source-dir release-staging --tag <tag>
```

输出包括：

- `sifli-kicad-libraries-<tag>.zip`
- `metadata-upstream.json`
- `package_path.txt`
- `metadata_path.txt`
- `package_size.txt`
- `install_size.txt`
- `package_sha256.txt`

## GitHub Actions Release Workflow

`/.github/workflows/release.yml` 在 tag push 时执行，固定顺序：

1. `actions/checkout@v4` with `submodules: recursive`
2. `astral-sh/setup-uv@v6`
3. `uv sync --frozen`
4. `uv run --directory SiliconSchema build-schema`
5. `uv run kicad-generator --output-dir build`
6. staging 发布目录整理
7. `uv run scripts/build_release.py --source-dir release-staging --tag <tag>`
8. `ncipollo/release-action` 上传 ZIP 与 `metadata-upstream.json`

## Notes

- CI 与本地都必须使用 `uv` 运行命令，不使用裸 `python`。
- 只要涉及 schema 变更，请优先验证 `SiliconSchema/out/<series>/series.yaml` 是否已刷新。
- 发布产物应由 CI 生成并上传，不建议手工维护 release ZIP。
