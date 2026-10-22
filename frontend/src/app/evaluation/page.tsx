'use client';

import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Input,
  Modal,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
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
import { useRequestSeq } from '@/hooks/useRequestSeq';
import type {
  EvalCaseResult,
  EvaluationHistoryItem,
  EvaluationReport,
  EvaluationRunItem,
  EvalTestCaseItem,
  EvalTestCaseOut,
  KnowledgeBase,
  QuestionTypeMetric,
  QuestionTypeOption,
} from '@/types';
import {
  confirmEvalTestCase,
  fetchEvalHistory,
  fetchEvalQuestionTypes,
  fetchEvalRuns,
  fetchEvalTestCases,
  fetchKnowledgeBases,
  runEvaluation,
  uploadTestSet,
} from '@/services/api';

const { Title, Paragraph, Text } = Typography;

/** Baseline 检索策略标识（与后端 BASELINE_RETRIEVAL_STRATEGY 一致）。 */
const BASELINE_STRATEGY = 'hybrid_rrf_rerank_hyde';

/** 测试集模板（点击"下载模板"生成，含问题分类与人工确认字段）。 */
const SAMPLE_TEST_SET = {
  dataset_version: 'tcm-v1',
  cases: [
    {
      question: '黄芪的性味、归经和主要功效是什么？',
      question_type: 'herb',
      golden_answer: '',
      golden_contexts: [],
      needs_review: true,
      source_reference: '中药资源「黄芪」条目；需由专业人员核对原文后填写标准答案',
    },
    {
      question: '2024年诺贝尔物理学奖的获得者是谁？',
      question_type: 'unanswerable',
      golden_answer:
        '根据现有资料，我无法回答该问题。知识库中未找到与您问题直接相关的内容，请尝试更换提问方式，或确认知识库中已包含相关文档。',
      golden_contexts: [],
      needs_review: false,
      source_reference: '系统拒答行为预期（非医学事实）',
    },
  ],
};

/** 百分比显示（0-1 → 整数）；null 视为 0。 */
const pct = (v: number | null | undefined) => Math.round((v || 0) * 100);

/** 颜色：达到阈值绿，未评估灰，否则红。 */
const scoreColor = (v: number | null | undefined, threshold: number) =>
  v == null ? '#8c8c8c' : v >= threshold ? '#52c41a' : '#ff4d4f';

/** 答案正确度单元格：null 显示"未评估"，区别于 0 分。 */
const AcProgress = ({ v, threshold }: { v: number | null; threshold: number }) =>
  v === null ? (
    <Tooltip title="标准答案缺失或待人工确认，该用例未参与答案正确度计算">
      <Tag>未评估</Tag>
    </Tooltip>
  ) : (
    <Progress percent={pct(v)} size="small" strokeColor={scoreColor(v, threshold)} />
  );

export default function EvaluationPage() {
  const reqSeq = useRequestSeq(); // BUG-048：切换知识库时丢弃过期响应
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKbId, setSelectedKbId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [history, setHistory] = useState<EvaluationHistoryItem[]>([]);
  const [runs, setRuns] = useState<EvaluationRunItem[]>([]);
  const [testCases, setTestCases] = useState<EvalTestCaseOut[]>([]);
  const [questionTypes, setQuestionTypes] = useState<QuestionTypeOption[]>([]);
  const [error, setError] = useState<string | null>(null);

  // TASK-009 实验维度：实验名 / 检索策略 / 测试集版本
  const [experimentName, setExperimentName] = useState('baseline');
  const [retrievalStrategy, setRetrievalStrategy] = useState(BASELINE_STRATEGY);
  const [datasetVersion, setDatasetVersion] = useState('tcm-v1');

  // 人工确认标准答案弹窗
  const [confirming, setConfirming] = useState<EvalTestCaseOut | null>(null);
  const [confirmAnswer, setConfirmAnswer] = useState('');
  const [confirmSource, setConfirmSource] = useState('');
  const [confirmReviewed, setConfirmReviewed] = useState(false);

  // 初始化：加载知识库列表、问题类型、历史评估、运行归档
  useEffect(() => {
    void loadKbs();
    void loadHistory();
    void loadRuns();
    void loadQuestionTypes();
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
      setHistory(await fetchEvalHistory(selectedKbId ?? undefined));
    } catch {
      // 历史加载失败不阻塞主流程
    }
  };

  const loadRuns = async () => {
    try {
      setRuns(await fetchEvalRuns(selectedKbId ?? undefined));
    } catch {
      /* noop */
    }
  };

  const loadTestCases = async () => {
    if (!selectedKbId) return;
    try {
      setTestCases(await fetchEvalTestCases(selectedKbId));
    } catch {
      /* noop */
    }
  };

  const loadQuestionTypes = async () => {
    try {
      setQuestionTypes(await fetchEvalQuestionTypes());
    } catch {
      /* noop */
    }
  };

  // 切换知识库后刷新历史、运行归档与测试集
  // BUG-048：旧实现串行 await 三个接口且无作废机制——快速切 KB 时，先发起的
  // 旧 KB 响应后到会覆盖新 KB 的结果。改为并发 + 请求序号守卫（过期即丢弃）。
  const handleKbChange = (kbId: string) => {
    setSelectedKbId(kbId);
    const reqId = reqSeq.begin();
    void (async () => {
      try {
        const [history, runs, cases] = await Promise.all([
          fetchEvalHistory(kbId),
          fetchEvalRuns(kbId),
          fetchEvalTestCases(kbId),
        ]);
        if (!reqSeq.isLatest(reqId)) return;
        setHistory(history);
        setRuns(runs);
        setTestCases(cases);
      } catch {
        /* noop */
      }
    })();
  };

  /** 解析并校验测试集 JSON（支持数组或 {cases: [...]} 两种格式）。 */
  const parseTestSet = (raw: string): { cases: EvalTestCaseItem[]; version?: string } => {
    const obj = JSON.parse(raw);
    const list = Array.isArray(obj) ? obj : obj?.cases;
    if (!Array.isArray(list)) {
      throw new Error('测试集必须是 JSON 数组，或包含 cases 数组的对象');
    }
    const cases: EvalTestCaseItem[] = list.map((it: Record<string, unknown>, idx: number) => {
      if (typeof it.question !== 'string' || !it.question.trim()) {
        throw new Error(`第 ${idx + 1} 条缺少非空 question 字段`);
      }
      return {
        question: it.question,
        golden_answer: typeof it.golden_answer === 'string' ? it.golden_answer : '',
        golden_contexts: Array.isArray(it.golden_contexts)
          ? it.golden_contexts.filter((c) => typeof c === 'string')
          : [],
        ...(typeof it.question_type === 'string' ? { question_type: it.question_type } : {}),
        ...(typeof it.dataset_version === 'string'
          ? { dataset_version: it.dataset_version }
          : {}),
        ...(typeof it.needs_review === 'boolean' ? { needs_review: it.needs_review } : {}),
        ...(typeof it.source_reference === 'string'
          ? { source_reference: it.source_reference }
          : {}),
      };
    });
    if (cases.length === 0) throw new Error('测试集为空');
    return {
      cases,
      version: !Array.isArray(obj) && typeof obj?.dataset_version === 'string'
        ? obj.dataset_version
        : undefined,
    };
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
      const parsed = parseTestSet(text);
      const version = parsed.version ?? datasetVersion;
      const res = await uploadTestSet(selectedKbId, parsed.cases, version);
      message.success(`已上传 ${res.uploaded} 条测试用例（数据集 ${res.dataset_version}）`);
      setTestCases(await fetchEvalTestCases(selectedKbId));
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
      const rep = await runEvaluation(selectedKbId, undefined, {
        experiment_name: experimentName || 'baseline',
        retrieval_strategy: retrievalStrategy || BASELINE_STRATEGY,
        dataset_version: datasetVersion || undefined,
      });
      setReport(rep);
      if (rep.evaluated_count === 0) {
        message.warning(
          `本次运行无已确认标准答案的用例（${rep.skipped_count} 条待人工标注），答案正确度未计算`,
        );
      } else if (rep.passed) {
        message.success(
          `评估通过：答案正确度 ${pct(rep.answer_correctness)}% ≥ ${pct(rep.threshold)}%`,
        );
      } else {
        message.warning(
          `评估未通过门禁：答案正确度 ${pct(rep.answer_correctness)}% < ${pct(rep.threshold)}%`,
        );
      }
      await loadHistory();
      await loadRuns();
    } catch (e) {
      const msg = e instanceof Error ? e.message : '评估运行失败';
      setError(msg);
      message.error(msg);
    } finally {
      setRunning(false);
    }
  };

  const openConfirm = (row: EvalTestCaseOut) => {
    setConfirming(row);
    setConfirmAnswer(row.golden_answer);
    setConfirmSource(row.source_reference);
    setConfirmReviewed(!row.needs_review);
  };

  const submitConfirm = async () => {
    if (!confirming) return;
    try {
      const updated = await confirmEvalTestCase(confirming.id, {
        golden_answer: confirmAnswer,
        source_reference: confirmSource,
        needs_review: !confirmReviewed,
      });
      setTestCases((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
      message.success(updated.needs_review ? '已标记为待人工确认' : '标准答案已确认，可参与评估');
      setConfirming(null);
    } catch (e) {
      message.error(e instanceof Error ? e.message : '保存失败');
    }
  };

  /** 下载测试集模板 JSON（含问题分类与人工确认字段）。 */
  const downloadSample = () => {
    const blob = new Blob([JSON.stringify(SAMPLE_TEST_SET, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'tcm_testset_template.json';
    a.click();
    URL.revokeObjectURL(url);
  };

  const threshold = report?.threshold ?? 0.75;
  const pendingCount = testCases.filter((c) => c.needs_review).length;

  const caseColumns: ColumnsType<EvalCaseResult> = [
    {
      title: '问题',
      dataIndex: 'question',
      key: 'question',
      width: 200,
      ellipsis: true,
    },
    {
      title: '问题类型',
      dataIndex: 'question_type',
      key: 'question_type',
      width: 110,
      render: (v: string) => (
        <Tag color="blue">
          {questionTypes.find((q) => q.value === v)?.label ?? v}
        </Tag>
      ),
    },
    {
      title: '生成答案',
      dataIndex: 'answer',
      key: 'answer',
      width: 220,
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
      width: 180,
      render: (g: string, row) =>
        g ? (
          <Text style={{ fontSize: 13 }}>{g}</Text>
        ) : (
          <Tooltip title="未填写标准答案，需人工确认后才会计算答案正确度">
            <Tag color="warning">待人工标注</Tag>
          </Tooltip>
        ),
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
      render: (v: number | null) => <AcProgress v={v} threshold={threshold} />,
    },
  ];

  const typeColumns: ColumnsType<QuestionTypeMetric> = [
    {
      title: '问题类型',
      dataIndex: 'question_type_label',
      key: 'question_type_label',
      width: 130,
    },
    { title: '用例数', dataIndex: 'case_count', key: 'case_count', width: 90 },
    {
      title: '已评估 / 跳过',
      key: 'evaluated',
      width: 130,
      render: (_, row) => (
        <Text style={{ fontSize: 13 }}>
          {row.evaluated_count} / {row.skipped_count}
        </Text>
      ),
    },
    {
      title: '上下文相关度',
      dataIndex: 'context_relevancy',
      key: 'context_relevancy',
      width: 150,
      render: (v: number) => (
        <Progress percent={pct(v)} size="small" strokeColor={scoreColor(v, threshold)} />
      ),
    },
    {
      title: '答案正确度',
      dataIndex: 'answer_correctness',
      key: 'answer_correctness',
      width: 150,
      render: (v: number | null) => <AcProgress v={v} threshold={threshold} />,
    },
  ];

  const runColumns: ColumnsType<EvaluationRunItem> = [
    { title: '实验名称', dataIndex: 'experiment_name', key: 'experiment_name', width: 160 },
    { title: '检索策略', dataIndex: 'retrieval_strategy', key: 'retrieval_strategy', width: 200 },
    { title: '测试集', dataIndex: 'dataset_version', key: 'dataset_version', width: 100 },
    { title: '用例数', dataIndex: 'case_count', key: 'case_count', width: 90 },
    {
      title: '已评估 / 跳过',
      key: 'evaluated',
      width: 130,
      render: (_, row) => (
        <Text style={{ fontSize: 13 }}>
          {row.evaluated_count} / {row.skipped_count}
        </Text>
      ),
    },
    {
      title: '上下文相关度',
      dataIndex: 'context_relevancy',
      key: 'context_relevancy',
      width: 140,
      render: (v: number) => <Progress percent={pct(v)} size="small" />,
    },
    {
      title: '答案正确度',
      dataIndex: 'answer_correctness',
      key: 'answer_correctness',
      width: 140,
      render: (v: number | null, row) => <AcProgress v={v} threshold={row.threshold} />,
    },
    {
      title: '运行时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (t: string | null) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '-'),
    },
  ];

  const testCaseColumns: ColumnsType<EvalTestCaseOut> = [
    { title: '问题', dataIndex: 'question', key: 'question', ellipsis: true },
    {
      title: '问题类型',
      dataIndex: 'question_type_label',
      key: 'question_type_label',
      width: 120,
      render: (v: string) => <Tag color="blue">{v}</Tag>,
    },
    {
      title: '标准答案',
      dataIndex: 'golden_answer',
      key: 'golden_answer',
      width: 220,
      ellipsis: true,
      render: (g: string) => g || <Text type="secondary">（空）</Text>,
    },
    {
      title: '标注状态',
      dataIndex: 'needs_review',
      key: 'needs_review',
      width: 110,
      render: (v: boolean) =>
        v ? <Tag color="warning">待人工确认</Tag> : <Tag color="success">已确认</Tag>,
    },
    {
      title: '操作',
      key: 'action',
      width: 110,
      render: (_, row) => (
        <Button type="link" size="small" onClick={() => openConfirm(row)}>
          人工确认
        </Button>
      ),
    },
  ];

  const historyColumns: ColumnsType<EvaluationHistoryItem> = [
    { title: '问题', dataIndex: 'question', key: 'question', ellipsis: true },
    {
      title: '问题类型',
      dataIndex: 'question_type_label',
      key: 'question_type_label',
      width: 110,
      render: (v: string | null) => (v ? <Tag color="blue">{v}</Tag> : '-'),
    },
    {
      title: '实验',
      dataIndex: 'experiment_name',
      key: 'experiment_name',
      width: 120,
      render: (v: string | null) => v ?? '-',
    },
    {
      title: '答案正确度',
      dataIndex: 'answer_correctness',
      key: 'answer_correctness',
      width: 130,
      render: (v: number | null) => <AcProgress v={v} threshold={threshold} />,
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
        下载测试集模板
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

      {/* TASK-009：实验维度配置（Baseline 与后续消融实验对比） */}
      <Card size="small" style={{ marginBottom: 16 }} title="实验配置">
        <Space size="large" wrap>
          <Space size={8}>
            <Text type="secondary">实验名称</Text>
            <Input
              style={{ width: 180 }}
              value={experimentName}
              onChange={(e) => setExperimentName(e.target.value)}
              placeholder="baseline"
            />
          </Space>
          <Space size={8}>
            <Text type="secondary">检索策略</Text>
            <Input
              style={{ width: 220 }}
              value={retrievalStrategy}
              onChange={(e) => setRetrievalStrategy(e.target.value)}
              placeholder={BASELINE_STRATEGY}
            />
          </Space>
          <Space size={8}>
            <Text type="secondary">测试集版本</Text>
            <Input
              style={{ width: 140 }}
              value={datasetVersion}
              onChange={(e) => setDatasetVersion(e.target.value)}
              placeholder="tcm-v1"
            />
          </Space>
          <Button onClick={() => void loadTestCases()}>刷新测试集</Button>
          <Button onClick={() => void loadRuns()}>刷新实验列表</Button>
        </Space>
        <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0, fontSize: 12 }}>
          实验名 / 检索策略 / 测试集版本会随运行结果一并归档，用于在相同测试集下对比不同检索策略。
          {pendingCount > 0 && (
            <>
              {' '}
              当前测试集有 <Text strong>{pendingCount}</Text> 条待人工确认标准答案，
              这些用例只计算上下文相关度，答案正确度记为「未评估」，不会按 0 分计入均值。
            </>
          )}
        </Paragraph>
      </Card>

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
                title={`答案正确度（已评估 ${report.evaluated_count} / 跳过 ${report.skipped_count}）`}
                value={report.answer_correctness === null ? '未评估' : pct(report.answer_correctness)}
                suffix={report.answer_correctness === null ? undefined : '%'}
                valueStyle={{ color: scoreColor(report.answer_correctness, threshold) }}
              />
            </Col>
            <Col xs={12} md={6}>
              <Statistic
                title={`质量门禁（≥${pct(threshold)}%）`}
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

          <Descriptions size="small" column={3} style={{ marginTop: 16 }} bordered>
            <Descriptions.Item label="实验名称">{report.experiment_name}</Descriptions.Item>
            <Descriptions.Item label="检索策略">{report.retrieval_strategy}</Descriptions.Item>
            <Descriptions.Item label="测试集版本">{report.dataset_version}</Descriptions.Item>
          </Descriptions>

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
                format={
                  report.answer_correctness === null ? () => '未评估' : (p) => `${p}%`
                }
              />
            </Col>
          </Row>

          {report.by_question_type.length > 0 && (
            <>
              <Title level={5} style={{ marginTop: 24 }}>
                按问题类型
              </Title>
              <Table
                rowKey="question_type"
                size="small"
                columns={typeColumns}
                dataSource={report.by_question_type}
                pagination={false}
              />
            </>
          )}

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

      {/* 实验运行归档 */}
      <Card
        title="实验运行归档"
        size="small"
        style={{ marginBottom: 16 }}
        extra={<Text type="secondary">同一测试集下对比不同检索策略</Text>}
      >
        <Table
          rowKey="run_id"
          size="small"
          columns={runColumns}
          dataSource={runs}
          pagination={{ pageSize: 10 }}
          locale={{ emptyText: '暂无实验运行记录' }}
          expandable={{
            expandedRowRender: (row) => (
              <div>
                <Table
                  rowKey="question_type"
                  size="small"
                  pagination={false}
                  columns={typeColumns}
                  dataSource={row.by_question_type}
                  locale={{ emptyText: '无按类型明细' }}
                />
                <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
                  配置快照：{JSON.stringify(row.config_snapshot)}
                </Paragraph>
              </div>
            ),
          }}
        />
      </Card>

      {/* 测试集与人工确认 */}
      <Card
        title="测试集"
        size="small"
        style={{ marginBottom: 16 }}
        extra={
          <Text type="secondary">
            共 {testCases.length} 条，待人工确认 {pendingCount} 条
          </Text>
        }
      >
        <Table
          rowKey="id"
          size="small"
          columns={testCaseColumns}
          dataSource={testCases}
          pagination={{ pageSize: 10 }}
          locale={{ emptyText: '尚未上传测试集' }}
        />
      </Card>

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

      <Modal
        title="人工确认标准答案"
        open={confirming !== null}
        onOk={() => void submitConfirm()}
        onCancel={() => setConfirming(null)}
        okText="保存"
        cancelText="取消"
      >
        {confirming && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Text strong>{confirming.question}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              标注依据提示：{confirming.source_reference || '（未填写）'}
            </Text>
            <Input.TextArea
              rows={6}
              value={confirmAnswer}
              onChange={(e) => setConfirmAnswer(e.target.value)}
              placeholder="请依据知识库原文填写标准答案，不要凭记忆编造医学事实"
            />
            <Input
              value={confirmSource}
              onChange={(e) => setConfirmSource(e.target.value)}
              placeholder="标注依据（如：中药资源「黄芪」条目）"
            />
            <Space>
              <Switch checked={confirmReviewed} onChange={setConfirmReviewed} />
              <Text style={{ fontSize: 13 }}>
                已人工确认（勾选后该用例参与答案正确度计算）
              </Text>
            </Space>
          </Space>
        )}
      </Modal>
    </AppLayout>
  );
}
