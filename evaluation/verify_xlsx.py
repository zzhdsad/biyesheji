"""B-08 验收：用真实 Excel 文件验证正式镜像的 openpyxl 解析能力。

只读脚本：不写数据库、不落盘，仅调用现有生产解析入口 document_extract.extract_xlsx_sheets。
用法：python verify_xlsx.py <xlsx 路径>
"""

import sys
from pathlib import Path

sys.path.insert(0, "/app")

import openpyxl  # 缺失时此处即抛 ModuleNotFoundError，作为验收失败信号

from src.infrastructure.document_extract import extract_xlsx_sheets


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python verify_xlsx.py <xlsx 路径>")
        return 2

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"文件不存在: {path}")
        return 2

    print(f"openpyxl 版本: {openpyxl.__version__}")
    content = path.read_bytes()
    print(f"文件: {path.name}  {len(content)} 字节")

    sheets, truncated = extract_xlsx_sheets(content, max_rows=50)
    print(f"工作表数: {len(sheets)}  是否截断: {truncated}")
    for sheet in sheets:
        print(f"  [{sheet['name']}] 解析到 {len(sheet['rows'])} 行（上限 50）")
        for row in sheet["rows"][:3]:
            print(f"    {row}")
    total = sum(len(s["rows"]) for s in sheets)
    print(f"总解析行数: {total}")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
