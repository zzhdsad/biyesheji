'use client';

import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
  type UploadProps,
} from 'antd';
import type { RcFile } from 'antd/es/upload';
import type { ColumnsType } from 'antd/es/table';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudUploadOutlined,
  DownloadOutlined,
  ExperimentOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';
import type {
  EvaluationHistoryItem,
  EvaluationReport,
  EvalTestCaseItem,
  KnowledgeBase,
} from '@/types';
import {
  fetchEvalHistory,
  fetchKnowledgeBases,
  runEvaluation,
  uploadTestSet,
} from '@/services/api';

const { Title, Paragraph, Text } = Typography;

/** 示例测试集（点击"下载示例"生成，便于用户套用格式）。 */
const SAMPLE_TEST_SET: EvalTestCaseItem[] = [
  {
    question: '公司实行什么工时制度？',
    golden_answer: '公司实行标准工时制，每日工作8小时，每周40小时。',
    golden_contexts: ['公司实行标准工时制，每日工作8小时，每周40小时。'],
  },
  {
    question: '年假有多少天？',
    golden_answer: '入职满一年享有5天带薪年假。',
    golden_contexts: ['员工入职满一年享有5天带薪年假。'],
  },
];

/** 百分比显示（0-1 → 整数）。 */
const pct = (v: number) => Math.round((v || 0) * 100);

/** 颜色：达到阈值绿，否则红。 */
const scoreColor = (v: number, threshold: number) =>
  v >= threshold ? '#52c41a' : '#ff4d4f';

export default function EvaluationPage() {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKbId, setSelectedKbId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [history, setHistory] = useState<EvaluationHistoryItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  // 初始化：加载知识库列表与历史评估
  useEffect(() => {
    void loadKbs();
    void loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadKbs = async () => {
    try {
      const kbs = await fetchKnowledgeBases();
      setKnowledgeBases(kbs);
      setSelectedKbId((prev) => prev ?? kbs[0]?.id ?? null);
    } catch {
      // 静默：选择器空态会提示
    }
  };

  const loadHistory = async () => {
    try {
      const list = await fetchEvalHistory(selectedKbId ?? undefined);
      setHistory(list);
    } catch {
      // 历史加载失败不阻塞主流程
    }
  };

  // 切换知识库后刷新历史
  const handleKbChange = (kbId: string) => {
    setSelectedKbId(kbId);
    void (async () => {
      try {
        setHistory(await fetchEvalHistory(kbId));
      } catch {
        /* noop */
      }
    })();
  };

  /** 解析并校验测试集 JSON 文件。 */
  const parseTestSet = (raw: string): EvalTestCaseItem[] => {
    const obj = JSON.parse(raw);
    if (!Array.isArray(obj)) {
      throw new Error('测试集必须是 JSON 数组');
    }
    const cases = obj.map((it: Record<string, unknown>, idx: number) => {
      if (typeof it.question !== 'string' || !it.question.trim()) {
        throw new Error(`第 ${idx + 1} 条缺少非空 question 字段`);
      }
      return {
        question: it.question,
        golden_answer: typeof it.golden_answer === 'string' ? it.golden_answer : '',
        golden_contexts: Array.isArray(it.golden_contexts)
          ? it.golden_contexts.filter((c) => typeof c === 'string')
          : [],
      };
    });
    if (cases.length === 0) throw new Error('测试集为空');
    return cases;
  };

  const handleUpload: UploadProps['beforeUpload'] = async (file) => {
    if (!selectedKbId) {
      message.error('请先选择知识库');
      return Upload.LIST_IGNORE;
    }
    setUploading(true);
    setError(null);
    try {
      const text = await (file as RcFile).text();
      const cases = parseTestSet(text);
      const res = await uploadTestSet(selectedKbId, cases);
      message.success(`已上传 ${res.uploaded} 条测试用例`);
      await loadHistory();
    } catch (e) {
      const msg = e instanceof Error ? e.message : '上传失败';
      setError(msg);
      message.error(msg);
    } finally {
      setUploading(false);
    }
    return Upload.LIST_IGNORE; // 阻止 antd 自动上传
  };

  const handleRun = async () => {
    if (!selectedKbId) {
      message.error('请先选择知识库');
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const rep = await runEvaluation(selectedKbId);
      setReport(rep);
      if (rep.passed) {
        message.success(`评估通过：答案正确度 ${pct(rep.answer_correctness)}% ≥ ${pct(rep.threshold)}%`);
      } else {
        message.warning(`评估未通过门禁：答案正确度 ${pct(rep.answer_correctness)}% < ${pct(rep.threshold)}%`);
      }
      await loadHistory();
    } catch (e) {
      const msg = e instanceof Error ? e.message : '评估运行失败';
      setError(msg);
      message.error(msg);
    } finally {
      setRunning(false);
    }
  };

  /** 下载示例测试集 JSON。 */
  const downloadSample = () => {
    const blob = new Blob([JSON.stringify(SAMPLE_TEST_SET, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'sample_testset.json';
    a.click();
    URL.revokeObjectURL(url);
  };

  const threshold = report?.threshold ?? 0.75;

  const caseColumns: ColumnsType<EvaluationReport['results'][number]> = [
    {
      title: '问题',
      dataIndex: 'question',
      key: 'question',
      width: 200,
      ellipsis: true,
    },
    {
      title: '生成答案',
      dataIndex: 'answer',
      key: 'answer',
      width: 240,
      ellipsis: { showTitle: false },
      render: (a: string, row) =>
        row.error ? (
          <Tooltip title={row.error}>
            <Tag color="error">生成失败</Tag>
          </Tooltip>
        ) : (
          <Tooltip title={a}>
            <Text type="secondary" style={{ fontSize: 13 }}>
              {a || '-'}
            </Text>
          </Tooltip>
        ),
    },
    {
      title: '标准答案',
      dataIndex: 'golden_answer',
      key: 'golden_answer',
      width: 200,
      ellipsis: true,
    },
    {
      title: '上下文相关度',
      dataIndex: 'context_relevancy',
      key: 'context_relevancy',
      width: 140,
      render: (v: number) => (
        <Progress
          percent={pct(v)}
          size="small"
          strokeColor={scoreColor(v, threshold)}
          format={(p) => `${p}%`}
        />
      ),
    },
    {
      title: '答案正确度',
      dataIndex: 'answer_correctness',
      key: 'answer_correctness',
      width: 140,
      render: (v: number) => (
        <Progress
          percent={pct(v)}
          size="small"
          strokeColor={scoreColor(v, threshold)}
          format={(p) => `${p}%`}
        />
      ),
    },
  ];

  const historyColumns: ColumnsType<EvaluationHistoryItem> = [
    { title: '问题', dataIndex: 'question', key: 'question', ellipsis: true },
    {
      title: '答案正确度',
      dataIndex: 'answer_correctness',
      key: 'answer_correctness',
      width: 130,
      render: (v: number) => (
        <Progress percent={pct(v)} size="small" strokeColor={scoreColor(v, threshold)} />
      ),
    },
    {
      title: '上下文相关度',
      dataIndex: 'context_relevancy',
      key: 'context_relevancy',
      width: 130,
      render: (v: number) => <Progress percent={pct(v)} size="small" />,
    },
    {
      title: '评估时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (t: string | null) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '-'),
    },
  ];

  const headerLeft = (
    <Space size={12} align="center">
      <ExperimentOutlined />
      <h2 style={{ margin: 0 }}>评估面板</h2>
      <Select
        style={{ width: 240 }}
        placeholder="选择知识库"
        value={selectedKbId ?? undefined}
        onChange={handleKbChange}
        options={knowledgeBases.map((kb) => ({ label: kb.name, value: kb.id }))}
        notFoundContent="暂无知识库"
      />
    </Space>
  );

  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<DownloadOutlined />} onClick={downloadSample}>
        下载示例测试集
      </Button>
      <Upload accept=".json" showUploadList={false} beforeUpload={handleUpload}>
        <Button icon={<CloudUploadOutlined />} loading={uploading} disabled={!selectedKbId}>
          上传测试集
        </Button>
      </Upload>
      <Button
        type="primary"
        icon={<ThunderboltOutlined />}
        loading={running}
        disabled={!selectedKbId}
        onClick={() => void handleRun()}
      >
        运行评估
      </Button>
      <AdminHeaderRight />
    </Space>
  );

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      {error && (
        <Alert
          type="error"
          message={error}
          closable
          showIcon
          style={{ marginBottom: 16 }}
          onClose={() => setError(null)}
        />
      )}

      {/* 评估报告 */}
      {report ? (
        <Card style={{ marginBottom: 16 }}>
          <Row gutter={[16, 16]}>
            <Col xs={12} md={6}>
              <Statistic title="用例数" value={report.case_count} prefix={<ExperimentOutlined />} />
            </Col>
            <Col xs={12} md={6}>
              <Statistic
                title="上下文相关度"
                value={pct(report.context_relevancy)}
                suffix="%"
                valueStyle={{ color: scoreColor(report.context_relevancy, threshold) }}
              />
            </Col>
            <Col xs={12} md={6}>
              <Statistic
                title="答案正确度"
                value={pct(report.answer_correctness)}
                suffix="%"
                valueStyle={{ color: scoreColor(report.answer_correctness, threshold) }}
              />
            </Col>
            <Col xs={12} md={6}>
              <Statistic
                title="质量门禁（≥75%）"
                valueRender={() =>
                  report.passed ? (
                    <Tag icon={<CheckCircleOutlined />} color="success" style={{ fontSize: 14 }}>
                      通过
                    </Tag>
                  ) : (
                    <Tag icon={<CloseCircleOutlined />} color="error" style={{ fontSize: 14 }}>
                      未通过
                    </Tag>
                  )
                }
              />
            </Col>
          </Row>

          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} md={12}>
              <div style={{ marginBottom: 4 }}>
                <Text type="secondary">上下文相关度</Text>
              </div>
              <Progress
                percent={pct(report.context_relevancy)}
                strokeColor={scoreColor(report.context_relevancy, threshold)}
                format={(p) => `${p}%`}
              />
            </Col>
            <Col xs={24} md={12}>
              <div style={{ marginBottom: 4 }}>
                <Text type="secondary">答案正确度（门禁线 {pct(threshold)}%）</Text>
              </div>
              <Progress
                percent={pct(report.answer_correctness)}
                strokeColor={scoreColor(report.answer_correctness, threshold)}
                format={(p) => `${p}%`}
              />
            </Col>
          </Row>

          <Title level={5} style={{ marginTop: 24 }}>
            逐条明细
          </Title>
          <Table
            rowKey="test_case_id"
            size="small"
            columns={caseColumns}
            dataSource={report.results}
            pagination={{ pageSize: 10 }}
            expandable={{
              expandedRowRender: (row) => (
                <div style={{ fontSize: 13 }}>
                  <Paragraph style={{ margin: '4px 0' }}>
                    <Text strong>检索上下文：</Text>
                    {row.retrieved_contexts.length > 0
                      ? row.retrieved_contexts.map((c, i) => (
                          <div key={i} style={{ color: '#595959', marginLeft: 8 }}>
                            [{i + 1}] {c.length > 120 ? c.slice(0, 120) + '…' : c}
                          </div>
                        ))
                      : '（无检索结果）'}
                  </Paragraph>
                </div>
              ),
            }}
          />
        </Card>
      ) : (
        <Card style={{ marginBottom: 16 }}>
          <Empty
            description="尚未运行评估。请上传测试集后点击「运行评估」"
            style={{ margin: '24px 0' }}
          />
        </Card>
      )}

      {/* 历史评估 */}
      <Card title="历史评估结果" size="small">
        <Table
          rowKey="id"
          size="small"
          columns={historyColumns}
          dataSource={history}
          pagination={{ pageSize: 10 }}
          locale={{ emptyText: '暂无历史评估记录' }}
        />
      </Card>
    </AppLayout>
  );
}
