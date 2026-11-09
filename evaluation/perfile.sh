#!/bin/sh
# 逐文件统计 pytest 结果，供本轮 Bug 诊断定位失败面
cd /app || exit 1
: > /tmp/perfile.txt
for f in tests/test_*.py; do
  r=$(timeout 150 pytest "$f" -q --tb=no 2>&1 | tail -1 | tr -d '\r')
  echo "$f | $r" >> /tmp/perfile.txt
done
echo DONE > /tmp/perfile.done
