"""生成浏览器端到端用的夹具文件（在容器内执行，openpyxl 可用）。"""

import os

from openpyxl import Workbook

OUT = "/tmp/fixtures"
os.makedirs(OUT, exist_ok=True)

# D1 术语
with open(f"{OUT}/D1术语.csv", "w", encoding="utf-8") as fh:
    fh.write("术语id,术语名称,解释\n")
    for i in range(1, 6):
        fh.write(f"T{i},脾胃虚弱{i},脾胃气虚导致的一系列症状{i}\n")

# D2 中成药
with open(f"{OUT}/D2中成药.csv", "w", encoding="utf-8") as fh:
    fh.write("中成药id,名称,功效\n")
    for i in range(1, 4):
        fh.write(f"M{i},四君子汤{i},益气健脾{i}\n")

# D3 关联表（M:N）
with open(f"{OUT}/D3关联.csv", "w", encoding="utf-8") as fh:
    fh.write("中成药id,术语id\n")
    fh.write("M1,T1\nM1,T2\nM2,T2\nM2,T3\n")

# 普通导入用：结构化 + 文档各一份
with open(f"{OUT}/普通-药材.csv", "w", encoding="utf-8") as fh:
    fh.write("药材id,药材名称,性味\n")
    for i in range(1, 4):
        fh.write(f"H{i},黄芪{i},甘温{i}\n")

with open(f"{OUT}/普通-说明.txt", "w", encoding="utf-8") as fh:
    fh.write("这是一份用于验证文档类导入的纯文本说明。\n中医理论知识库补充说明。\n")

# GBK 编码文件（验证编码识别）
with open(f"{OUT}/普通-GBK.csv", "w", encoding="gb18030") as fh:
    fh.write("编码,说明\n")
    fh.write("GBK,中文编码验证\n")

# 一个不支持的文件（验证错误展示）
with open(f"{OUT}/不支持.bin", "wb") as fh:
    fh.write(b"\x00\x01\x02\x03\x04")

# XLSX（验证真实 Excel 解析）
wb = Workbook()
ws = wb.active
ws.append(["成分id", "成分名称", "分子式"])
for i in range(1, 4):
    ws.append([f"C{i}", f"黄酮{i}", f"C{i}H{i}O"])
wb.save(f"{OUT}/化学成分.xlsx")

print("OK", sorted(os.listdir(OUT)))
