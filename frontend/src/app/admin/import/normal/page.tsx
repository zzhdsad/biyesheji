'use client';

import {
  CloudUploadOutlined,
  ImportOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Divider,
  Empty,
  Input,
  Progress,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { AdminHeaderRight } from '@/components/layout/AppSider';
import { AppLayout } from '@/components/layout/AppLayout';
import FilePicker, { type PickedFile } from '@/components/admin/FilePicker';
import ImportProgressBar from '@/components/admin/ImportProgressBar';
import { useImportProgressPolling } from '@/hooks/useImportProgressPolling';
import {
  commitGeneralImport,
  fetchKnowledgeBases,
  scanGeneralFiles,
  type GeneralCommitResult,
  type ScannedFile,
} from '@/services/api';
import { useNormalImportStore } from '@/stores/importStore';
import { describeApiError } from '@/utils/apiError';

const DOC_FORMATS = ['pdf', 'docx', 'txt', 'md'];
const FORMAT_COLOR: Record<string, string> = {
  csv: 'blue',
  tsv: 'blue',
  json: 'cyan',
  jsonl: 'cyan',
  xlsx: 'green',
  parquet: 'purple',
  pdf: 'red',
  docx: 'geekblue',
  txt: 'default',
  md: 'default',
};

export default function NormalImportPage() {
  const {
    kbId,
    sessionId,
    scanned,
    scanErrors,
    settings,
    committing,
    commitProgress,
    result,
    patch,
    reset,
  } = useNormalImportStore();

  const [picked, setPicked] = useState<PickedFile[]>([]);
  // FilePicker 的文件列表在组件内部，重置时需要重建才能一起清空
  const [pickerKey, setPickerKey] = useState(0);
  const [scanning, setScanning] = useState(false);
  const [scanProgress, setScanProgress] = useState(0);

  const { data: kbs = [] } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: fetchKnowledgeBases,
  });

  // 提交期间轮询服务端进度；重新挂载时 committing 为 true 会自动恢复
  useImportProgressPolling({
    sessionId,
    active: committing,
    onSnapshot: (snapshot) => patch({ commitProgress: snapshot }),
    onDone: (snapshot) => {
      patch({
        committing: false,
        commitProgress: snapshot,
        result: (snapshot.result as GeneralCommitResult | undefined) ?? result,
      });
    },
    onFailed: (snapshot) => {
      if (snapshot.failed) {
        patch({ committing: false, commitProgress: snapshot });
        message.error(`导入失败：${snapshot.message || '未知错误'}`);
      } else {
        // 任务不存在（会话过期/服务重启）：隐藏进度条，避免 0% 卡在界面上
        patch({ committing: false, commitProgress: null });
        message.warning(snapshot.message || '未找到进行中的导入任务');
      }
    },
  });

  const defaultSetting = useCallback(
    (path: string) => {
      if (settings[path]) return settings[path];
      // 用扫描得到的主键候选预填：不预填的话用户没手填主键就会静默生成 0 个实体
      const scannedFile = scanned.find((f) => f.path === path);
      const best = scannedFile?.primary_key_candidates?.[0]?.fields?.[0];
      return {
        entity_type: path.replace(/\.[^.]+$/, ''),
        pk_field: best ?? '',
      };
    },
    [settings, scanned],
  );

  const updateSetting = useCallback(
    (path: string, field: 'entity_type' | 'pk_field', value: string) => {
      patch({
        settings: {
          ...settings,
          [path]: { ...defaultSetting(path), [field]: value },
        },
      });
    },
    [defaultSetting, patch, settings],
  );

  const resetAll = () => {
    if (committing) {
      message.warning('导入正在进行中，完成后再清空');
      return;
    }
    reset();
    setPicked([]);
    setPickerKey((key) => key + 1);
    setScanProgress(0);
  };

  const runScan = async () => {
    if (picked.length === 0) {
      message.warning('请先选择文件或文件夹');
      return;
    }
    setScanning(true);
    setScanProgress(10);
    try {
      const data = await scanGeneralFiles(
        picked.map((p) => p.file),
        picked.map((p) => p.path),
      );
      // 扫描后把默认值**写进 store**，而不是留到渲染时派生：
      // 渲染时派生会受闭包/记忆化影响，实测出现过输入框显示为空、
      // 但提交时取到预填值的不一致；显式写入保证「所见即所提交」。
      const nextSettings: Record<string, { entity_type: string; pk_field: string }> = {};
      for (const item of data.files) {
        if (DOC_FORMATS.includes(item.detected_format)) continue;
        nextSettings[item.path] = {
          entity_type: item.path.replace(/\.[^.]+$/, ''),
          pk_field: item.primary_key_candidates?.[0]?.fields?.[0] ?? '',
        };
      }
      patch({
        sessionId: data.session_id,
        scanned: data.files,
        scanErrors: data.errors,
        settings: nextSettings,
        result: undefined,
        commitProgress: null,
        committing: false,
      });
      setScanProgress(100);
      const usable = data.files.filter((f) => f.supported).length;
      if (usable === 0) {
        message.error(`扫描失败：0 / ${data.files.length} 个文件可用`);
      } else if (usable < data.files.length) {
        message.warning(`扫描完成：${usable} / ${data.files.length} 个文件可用，其余文件存在错误`);
      } else {
        message.success(`扫描完成：${usable} 个文件可用`);
      }
    } catch (error) {
      message.error(`扫描失败：${describeApiError(error)}`);
      setScanProgress(0);
    } finally {
      setScanning(false);
    }
  };

  const runCommit = async () => {
    if (!kbId) {
      message.warning('请选择目标知识库');
      return;
    }
    if (!sessionId) {
      message.error('导入会话已失效，请重新扫描文件');
      return;
    }
    const usable = scanned.filter((f) => f.supported && !f.error);
    if (usable.length === 0) {
      message.warning('没有可导入的文件');
      return;
    }
    patch({ committing: true, commitProgress: null });
    try {
      const data = await commitGeneralImport({
        session_id: sessionId,
        kb_id: kbId,
        files: usable.map((f) => {
          const setting = defaultSetting(f.path);
          return {
            path: f.path,
            entity_type: setting.entity_type,
            pk_field: setting.pk_field || null,
          };
        }),
      });
      patch({ result: data, committing: false, commitProgress: null });
      const imported = data.documents + data.records;
      const summary = `文档 ${data.documents}、记录 ${data.records}、实体 ${data.entities}，失败 ${data.failed}`;
      if (data.failed > 0 && imported === 0) {
        message.error(`导入失败：${summary}`);
      } else if (data.failed > 0) {
        message.warning(`导入完成但存在失败：${summary}`);
      } else {
        message.success(`导入完成：${summary}`);
      }
    } catch (error) {
      patch({ committing: false });
      message.error(`导入失败：${describeApiError(error)}`);
    }
  };

  const columns: ColumnsType<ScannedFile> = useMemo(
    () => [
      {
        title: '相对路径',
        dataIndex: 'path',
        ellipsis: true,
        render: (value: string, record) => (
          <Space size={4}>
            <Tag color={FORMAT_COLOR[record.detected_format] ?? 'default'}>
              {record.detected_format || '未知'}
            </Tag>
            <Typography.Text code ellipsis>
              {value}
            </Typography.Text>
          </Space>
        ),
      },
      {
        title: '编码',
        dataIndex: 'encoding',
        width: 90,
        render: (value: string) => value || '—',
      },
      {
        title: '记录数',
        dataIndex: 'row_count',
        width: 90,
      },
      {
        title: '实体类型 / 主键',
        width: 260,
        render: (_, record) => {
          if (DOC_FORMATS.includes(record.detected_format)) {
            return <Typography.Text type="secondary">文档类（走 RAG 链路）</Typography.Text>;
          }
          const setting = defaultSetting(record.path);
          return (
            <Space size={4}>
              <Input
                size="small"
                style={{ width: 120 }}
                placeholder="实体类型"
                value={setting.entity_type}
                onChange={(e) =>
                  updateSetting(record.path, 'entity_type', e.target.value)
                }
              />
              <Input
                size="small"
                style={{ width: 110 }}
                placeholder="主键字段"
                value={setting.pk_field}
                onChange={(e) => updateSetting(record.path, 'pk_field', e.target.value)}
              />
            </Space>
          );
        },
      },
      {
        title: '状态',
        width: 200,
        render: (_, record) =>
          record.error ? (
            <Typography.Text type="danger" ellipsis={{ tooltip: record.error }}>
              {record.error}
            </Typography.Text>
          ) : (
            <Tag color="success">就绪</Tag>
          ),
      },
    ],
    // 依赖必须包含 scanned：否则列渲染闭包里拿到的是扫描前的空数组，
    // 输入框显示为空（实际提交时取的却是新值），用户会以为主键没设置。
    [defaultSetting, updateSetting],
  );

  const previewColumns: ColumnsType<{ row_no: number; data: Record<string, unknown> }> =
    useMemo(() => {
      const first = scanned.find((f) => f.preview.length > 0);
      const keys = Object.keys(first?.preview[0]?.data ?? {});
      return [
        { title: '行号', dataIndex: 'row_no', width: 70 },
        ...keys.map((key) => ({
          title: key,
          key,
          ellipsis: true,
          render: (_: unknown, row: { data: Record<string, unknown> }) =>
            String(row.data[key] ?? ''),
        })),
      ] as ColumnsType<{ row_no: number; data: Record<string, unknown> }>;
    }, [scanned]);

  return (
    <AppLayout headerRight={<AdminHeaderRight />}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div>
          <Typography.Title level={4} style={{ marginBottom: 4 }}>
            普通数据导入
          </Typography.Title>
          <Typography.Text type="secondary">
            支持多文件与文件夹递归上传。文档类（PDF/DOCX/TXT/MD）进入既有 RAG 链路；
            结构化文件（CSV/TSV/JSON/JSONL/XLSX/Parquet）按记录入库，保留原始字段与行号。
          </Typography.Text>
        </div>

        <Card title="1. 选择文件">
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <FilePicker key={pickerKey} onChange={setPicked} />
            {sessionId && scanned.length > 0 && picked.length === 0 && (
              <Alert
                type="info"
                showIcon
                message="已恢复上次扫描结果"
                description="文件对象不会随页面切换保留，但扫描结果与导入会话仍在，可直接点击「开始导入」；需要换文件时重新选择并扫描即可覆盖。"
              />
            )}
          </Space>
        </Card>

        <Card
          title="2. 扫描与预览"
          extra={
            <Button size="small" icon={<ReloadOutlined />} onClick={resetAll} disabled={scanning}>
              清空本次导入
            </Button>
          }
        >
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Space wrap>
              <Select
                style={{ width: 260 }}
                placeholder="选择目标知识库"
                value={kbId}
                onChange={(value) => patch({ kbId: value })}
                disabled={committing}
                options={kbs.map((kb) => ({ label: kb.name, value: kb.id }))}
              />
              <Button
                type="primary"
                icon={<CloudUploadOutlined />}
                loading={scanning}
                onClick={runScan}
                disabled={picked.length === 0 || committing}
              >
                扫描文件
              </Button>
              {scanProgress > 0 && scanProgress < 100 && (
                <Progress percent={scanProgress} style={{ width: 200 }} />
              )}
            </Space>

            {scanErrors.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={`${scanErrors.length} 个文件存在错误`}
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {scanErrors.slice(0, 10).map((err, index) => (
                      <li key={index}>{err}</li>
                    ))}
                  </ul>
                }
              />
            )}

            {scanned.length === 0 ? (
              <Empty description="扫描后在此查看文件类型、编码与预览" />
            ) : (
              <>
                <Table
                  rowKey="path"
                  size="small"
                  columns={columns}
                  dataSource={scanned}
                  pagination={false}
                  scroll={{ x: 900 }}
                />
                <Divider orientation="left" plain>
                  数据预览（前 5 行，保留原始字段）
                </Divider>
                <Table
                  rowKey="row_no"
                  size="small"
                  columns={previewColumns}
                  dataSource={scanned.find((f) => f.preview.length > 0)?.preview ?? []}
                  pagination={false}
                  scroll={{ x: 700 }}
                />
              </>
            )}
          </Space>
        </Card>

        <Card title="3. 确认导入">
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Space wrap>
              <Button
                type="primary"
                icon={<ImportOutlined />}
                loading={committing}
                disabled={!kbId || scanned.length === 0 || committing || Boolean(result)}
                onClick={runCommit}
              >
                开始导入
              </Button>
              {result && (
                <Typography.Text type="secondary">
                  本次会话已完成导入；如需再次导入请重新扫描文件。
                </Typography.Text>
              )}
            </Space>

            {(committing || commitProgress) && (
              <ImportProgressBar progress={commitProgress} active={committing} />
            )}
            {committing && !commitProgress && (
              <Alert
                type="info"
                showIcon
                message="正在恢复导入进度"
                description="上次提交仍在服务端执行，进度会在下一次轮询后显示；离开页面不会中断导入。"
              />
            )}

            {result && (
              <>
                <Space size="large" wrap>
                  <Statistic title="文档" value={result.documents} />
                  <Statistic title="原始记录" value={result.records} />
                  <Statistic title="实体" value={result.entities} />
                  <Statistic
                    title="失败"
                    value={result.failed}
                    valueStyle={{
                      color: result.failed > 0 ? '#cf1322' : '#3f8600',
                    }}
                  />
                </Space>
                {result.errors.length > 0 && (
                  <Descriptions
                    bordered
                    size="small"
                    column={1}
                    title="失败明细"
                    items={result.errors.slice(0, 20).map((item, index) => ({
                      key: String(index),
                      label: `${item.path}${item.row_no ? ` 第 ${item.row_no} 行` : ''}`,
                      children: item.message,
                    }))}
                  />
                )}
              </>
            )}
          </Space>
        </Card>
      </Space>
    </AppLayout>
  );
}
