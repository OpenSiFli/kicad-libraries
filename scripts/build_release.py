#!/usr/bin/env python3
"""
SiFli KiCAD Library Release Builder

This script builds a release package for the SiFli KiCAD library according to
KiCAD addon specifications and prepares metadata for upstream submission.
"""

import os
import sys
import json
import zipfile
import hashlib
import subprocess
import shutil
from pathlib import Path
from typing import List, Dict, Any


def get_all_tags() -> List[str]:
    """获取仓库中的所有标签，按版本号排序"""
    try:
        result = subprocess.run(
            ['git', 'tag', '--sort=-version:refname'],
            capture_output=True,
            text=True,
            check=True
        )
        tags = [tag.strip() for tag in result.stdout.strip().split('\n') if tag.strip()]
        return tags
    except subprocess.CalledProcessError as e:
        print(f"Error getting tags: {e}")
        return []


def get_current_tag() -> str:
    """获取当前的标签"""
    current_tag = os.environ.get('GITHUB_REF_NAME', '')
    if not current_tag:
        try:
            result = subprocess.run(
                ['git', 'describe', '--tags', '--exact-match', 'HEAD'],
                capture_output=True,
                text=True,
                check=True
            )
            current_tag = result.stdout.strip()
        except subprocess.CalledProcessError:
            print("Error: Not on a tagged commit")
            sys.exit(1)
    return current_tag


def get_repo_info() -> Dict[str, str]:
    """获取仓库信息"""
    repo_url = os.environ.get('GITHUB_SERVER_URL', 'https://github.com')
    repo_name = os.environ.get('GITHUB_REPOSITORY', 'OpenSiFli/kicad-libraries')
    return {
        'url': repo_url,
        'name': repo_name,
        'download_base': f"{repo_url}/{repo_name}/releases/download"
    }


def calculate_file_hash(file_path: Path) -> str:
    """计算文件的SHA256哈希值"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def calculate_directory_size(directory: Path) -> int:
    """计算目录的总大小"""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(directory):
        for filename in filenames:
            file_path = Path(dirpath) / filename
            total_size += file_path.stat().st_size
    return total_size


def create_package_metadata(all_tags: List[str], current_tag: str) -> Dict[str, Any]:
    """创建打包用的metadata.json"""
    with open('metadata.json', 'r', encoding='utf-8') as f:
        metadata = json.load(f)
    
    # 添加versions字段
    versions = []
    for tag in all_tags:
        version_info = {
            "version": tag,
            "status": "stable" if tag == current_tag else "stable",
            "kicad_version": "9.0"
        }
        versions.append(version_info)
    
    metadata["versions"] = versions
    return metadata


def create_upstream_metadata(all_tags: List[str], current_tag: str, 
                           package_info: Dict[str, Any]) -> Dict[str, Any]:
    """创建提交到KiCAD上游的metadata.json"""
    with open('metadata.json', 'r', encoding='utf-8') as f:
        metadata = json.load(f)
    
    repo_info = get_repo_info()
    
    # 添加带下载信息的versions字段
    versions = []
    for tag in all_tags:
        version_info = {
            "version": tag,
            "status": "stable",
            "kicad_version": "6.0"
        }
        
        # 为当前tag添加下载信息
        if tag == current_tag:
            package_filename = f"kicad-libraries-{tag}.zip"
            download_url = f"{repo_info['download_base']}/{tag}/{package_filename}"
            
            version_info.update({
                "download_url": download_url,
                "download_sha256": package_info['sha256'],
                "download_size": package_info['size'],
                "install_size": package_info['install_size']
            })
        
        versions.append(version_info)
    
    metadata["versions"] = versions
    return metadata


def create_library_package(current_tag: str) -> Dict[str, Any]:
    """创建KiCAD库包"""
    # 定义需要包含的目录和文件
    include_items = [
        'metadata.json',
        'symbols',
        'footprints', 
        'resources'
    ]
    
    # 检查3dmodels目录是否存在
    if Path('3dmodels').exists():
        include_items.append('3dmodels')
    
    # 创建临时目录
    temp_dir = Path('temp_package')
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir()
    
    try:
        # 获取所有tags并创建打包用的metadata
        all_tags = get_all_tags()
        package_metadata = create_package_metadata(all_tags, current_tag)
        
        # 写入修改后的metadata.json到临时目录
        with open(temp_dir / 'metadata.json', 'w', encoding='utf-8') as f:
            json.dump(package_metadata, f, indent=2, ensure_ascii=False)
        
        # 复制其他文件和目录
        for item in include_items:
            if item == 'metadata.json':
                continue  # 已经处理过了
                
            item_path = Path(item)
            if item_path.exists():
                if item_path.is_dir():
                    shutil.copytree(item_path, temp_dir / item)
                else:
                    shutil.copy2(item_path, temp_dir / item)
        
        # 计算安装大小
        install_size = calculate_directory_size(temp_dir)
        
        # 创建zip包
        package_filename = f"kicad-libraries-{current_tag}.zip"
        package_path = Path(package_filename)
        
        with zipfile.ZipFile(package_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    file_path = Path(root) / file
                    # 计算相对于temp_dir的路径
                    arcname = file_path.relative_to(temp_dir)
                    zipf.write(file_path, arcname)
        
        # 计算包信息
        package_size = package_path.stat().st_size
        package_sha256 = calculate_file_hash(package_path)
        
        # 创建上游metadata
        upstream_metadata = create_upstream_metadata(all_tags, current_tag, {
            'size': package_size,
            'sha256': package_sha256,
            'install_size': install_size
        })
        
        # 保存上游metadata
        upstream_metadata_path = Path('metadata-upstream.json')
        with open(upstream_metadata_path, 'w', encoding='utf-8') as f:
            json.dump(upstream_metadata, f, indent=2, ensure_ascii=False)
        
        # 输出信息供GitHub Actions使用
        with open('package_path.txt', 'w') as f:
            f.write(str(package_path.absolute()))
        
        with open('metadata_path.txt', 'w') as f:
            f.write(str(upstream_metadata_path.absolute()))
        
        with open('package_size.txt', 'w') as f:
            f.write(str(package_size))
        
        with open('install_size.txt', 'w') as f:
            f.write(str(install_size))
        
        with open('package_sha256.txt', 'w') as f:
            f.write(package_sha256)
        
        print(f"✅ Package created: {package_filename}")
        print(f"📦 Package size: {package_size:,} bytes")
        print(f"💾 Install size: {install_size:,} bytes") 
        print(f"🔒 SHA256: {package_sha256}")
        print(f"📋 Upstream metadata: {upstream_metadata_path}")
        
        return {
            'package_path': package_path,
            'metadata_path': upstream_metadata_path,
            'size': package_size,
            'install_size': install_size,
            'sha256': package_sha256
        }
        
    finally:
        # 清理临时目录
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def main():
    """主函数"""
    print("🚀 Starting SiFli KiCAD Library release build...")
    
    # 检查必要文件
    if not Path('metadata.json').exists():
        print("❌ Error: metadata.json not found")
        sys.exit(1)
    
    # 获取当前标签
    current_tag = get_current_tag()
    print(f"📌 Current tag: {current_tag}")
    
    # 创建包
    try:
        package_info = create_library_package(current_tag)
        print(f"✅ Release build completed successfully!")
        
    except Exception as e:
        print(f"❌ Error during build: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
