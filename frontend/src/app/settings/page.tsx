'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  ApiOutlined,
  ExperimentOutlined,
  ReloadOutlined,
  RobotOutlined,
  SaveOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import type {
  EmbeddingDependencyState,
  EmbeddingStatus,
  ModelConfig,
  RevectorizeJobStatus,
} from '@/services/api';
import type { SystemConfigValues } from '@/types';
import { fetchModelConfig, testModelConnection, updateModelConfig, fetchSystemConfig, updateSystemConfig, fetchEmbeddingStatus, startRevectorize, fetchRevectorizeStatus, retryRevectorize, cancelRevectorize, resetSystemConfig } from '@/services/api';
import {
  canCancelJob,
  canStartRevectorize,
  dependencyColor,
  dependencyLabel,
  formatJobSummary,
  jobStatusLabel,
  shouldPollJob,
} from '@/utils/embeddingStatus';
import { describeApiError } from '@/utils/apiError';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';

const LLM_PROVIDER_OPTIONS = [
  { label: 'DeepSeek', value: 'deepseek' },
  { label: 'OpenAI', value: 'openai' },
  { label: 'Qwen / 通义千问', value: 'qwen' },
  { label: 'Ollama（本地）', value: 'ollama' },
  { label: 'vLLM / 自定义 OpenAI 兼容', value: 'custom' },
];

/** 各提供商默认 Base URL，选择时自动填充。 */
const PROVIDER_DEFAULTS: Record<string, { base_url: string; model: string }> = {
  deepseek: { base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  openai: { base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  qwen: { base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
  ollama: { base_url: 'http://localhost:11434/v1', model: 'qwen2.5:7b' },
  custom: { base_url: 'http://localhost:8000/v1', model: '' },
};

export default function SettingsPage() {
  const [form] = Form.useForm<ModelConfig>();
  const [sysForm] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [sysLoading, setSysLoading] = useState(false);
  const [sysSaving, setSysSaving] = useState(false);
  const [sysResetting, setSysResetting] = useState(false);
  // 后端返回的各字段默认值（用于界面提示与"恢复默认值"）
  const [sysDefaults, setSysDefaults] = useState<Partial<SystemConfigValues> | undefined>(
    undefined,
  );

  // ── Embedding 状态 / 批量重新向量化 ──
  const [embStatus, setEmbStatus] = useState<EmbeddingStatus | null>(null);
  const [embBusy, setEmbBusy] = useState(false);

  // API Key 是否已保存（后端持久化在 model_configs，脱敏回显）
  const [apiKeyState, setApiKeyState] = useState<{ configured: boolean; masked: string }>({
    configured: false,
    masked: '',
  });

  const loadEmbeddingStatus = useCallback(async () => {
    try {
      setEmbStatus(await fetchEmbeddingStatus());
    } catch {
      setEmbStatus(null); // 状态是辅助信息，加载失败不打断配置主流程
    }
  }, []);

  useEffect(() => {
    void loadEmbeddingStatus();
  }, [loadEmbeddingStatus]);

  const jobStatus = embStatus?.job?.status ?? null;
  // 依赖与模型权重随镜像内置，状态仅反映"是否在重新向量化"
  const dependencyState: EmbeddingDependencyState =
    embStatus?.overall_status === 'revectorizing' ? 'revectorizing' : 'ready';

  // 重新向量化进行中 → 轮询进度（结束后自动停止）
  useEffect(() => {
    if (!shouldPollJob(jobStatus)) return;
    const timer = setInterval(() => void loadEmbeddingStatus(), 2000);
    return () => clearInterval(timer);
  }, [jobStatus, loadEmbeddingStatus]);

  /** 批量重新向量化：把旧模型生成的存量向量一次性重建。 */
  const handleRevectorize = async () => {
    setEmbBusy(true);
    try {
      const job: RevectorizeJobStatus = await startRevectorize();
      message.success(`已开始重建 ${job.total} 篇文档的向量`);
      await loadEmbeddingStatus();
    } catch (err) {
      message.error(`发起失败：${describeError(err)}`);
    } finally {
      setEmbBusy(false);
    }
  };

  /** 只重试上一轮的失败文档。 */
  const handleRetryRevectorize = async () => {
    const jobId = embStatus?.job?.job_id;
    if (!jobId) return;
    setEmbBusy(true);
    try {
      await retryRevectorize(jobId);
      message.success('已开始重试失败文档');
      await loadEmbeddingStatus();
    } catch (err) {
      message.error(`重试失败：${describeError(err)}`);
    } finally {
      setEmbBusy(false);
    }
  };

  /** 取消进行中的批量任务（已完成的文档保留新向量，剩余文档不受影响）。 */
  const handleCancelRevectorize = async () => {
    const jobId = embStatus?.job?.job_id;
    if (!jobId) return;
    setEmbBusy(true);
    try {
      await cancelRevectorize(jobId);
      message.success('已请求取消，任务将在当前文档处理完成后停止');
      await loadEmbeddingStatus();
    } catch (err) {
      message.error(`取消失败：${describeError(err)}`);
      await loadEmbeddingStatus();
    } finally {
      setEmbBusy(false);
    }
  };

  // BUG-073（lint）：两个加载函数被下面挂载 effect 依赖。
  // message 这里是 antd 的静态导入（引用恒定），因此 useCallback 依赖为空数组：
  // 引用稳定 → effect 仍只在挂载时执行一次，行为不变。
  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const cfg = await fetchModelConfig();
      form.setFieldsValue(cfg);
      // API Key 只回显脱敏值；用"已配置 / 未配置"提示当前状态，
      // 让用户不必为了"是否已保存"而重新输入一遍。
      setApiKeyState({
        configured: Boolean(cfg.llm_api_key_configured),
        masked: cfg.llm_api_key_masked || cfg.llm_api_key || '',
      });
    } catch {
      message.error('加载模型配置失败');
    } finally {
      setLoading(false);
    }
  }, [form]);

  const loadSysConfig = useCallback(async () => {
    setSysLoading(true);
    try {
      const cfg = await fetchSystemConfig();
      sysForm.setFieldsValue(cfg);
      setSysDefaults(cfg.defaults);
    } catch {
      message.error('加载系统配置失败');
    } finally {
      setSysLoading(false);
    }
  }, [sysForm]);

  useEffect(() => {
    void loadConfig();
    void loadSysConfig();
  }, [loadConfig, loadSysConfig]);

  /** 表单项下方的"默认值："提示（取值来自后端 Settings 默认值）。 */
  const defaultHint = (field: keyof SystemConfigValues) => {
    const value = sysDefaults?.[field];
    return value === undefined ? undefined : `默认值：${value}`;
  };

  /** 恢复系统配置为后端默认值（不含 conversational 等无关状态）。 */
  const onSysReset = async () => {
    setSysResetting(true);
    try {
      const cfg = await resetSystemConfig();
      sysForm.setFieldsValue(cfg);
      setSysDefaults(cfg.defaults);
      message.success('已恢复为默认值');
    } catch (err) {
      message.error(`恢复失败：${describeError(err)}`);
    } finally {
      setSysResetting(false);
    }
  };

  const onSysSave = async () => {
    let values;
    try {
      values = await sysForm.validateFields();
    } catch {
      return;
    }
    setSysSaving(true);
    try {
      await updateSystemConfig(values);
      message.success('系统配置已保存');
    } catch (err) {
      message.error(`保存失败：${describeError(err)}`);
    } finally {
      setSysSaving(false);
    }
  };

  // 切换 LLM 提供商时自动填充默认 Base URL 和模型名
  const onProviderChange = (provider: string) => {
    const def = PROVIDER_DEFAULTS[provider];
    if (def) {
      form.setFieldsValue({
        llm_base_url: def.base_url,
        llm_model: def.model,
      });
    }
  };

  /** 从 axios 错误中提取后端返回的可读信息（兼容 AppException 的 {code,message} 体）。 */
  const describeError = describeApiError;

  const onTest = async () => {
    let values: Partial<ModelConfig>;
    try {
      values = await form.validateFields([
        'llm_provider',
        'llm_base_url',
        'llm_model',
        'llm_api_key',
      ]);
    } catch {
      return; // 表单校验失败，antd 已在字段下方标红
    }
    setTesting(true);
    setTestResult(null);
    try {
      const res = await testModelConnection(values);
      setTestResult({ ok: res.ok, msg: res.message });
      if (res.ok) {
        message.success(`连通成功（${res.latency_ms}ms）`);
      } else {
        message.error(res.message);
      }
    } catch (err) {
      const msg = describeError(err);
      setTestResult({ ok: false, msg });
      message.error(`连通测试失败：${msg}`);
    } finally {
      setTesting(false);
    }
  };

  const onSave = async () => {
    let values: Partial<ModelConfig>;
    try {
      values = await form.validateFields();
    } catch {
      message.warning('请先补全标红的必填项');
      return;
    }
    setSaving(true);
    try {
      await updateModelConfig(values);
      message.success('模型配置已保存，下次问答生效');
      // 保存后重新加载（API key 会变成脱敏值）
      await loadConfig();
    } catch (err) {
      message.error(`保存失败：${describeError(err)}`);
    } finally {
      setSaving(false);
    }
  };

  const headerLeft = (
    <Space size="middle" align="center">
      <SettingOutlined style={{ fontSize: 20 }} />
      <h2 style={{ margin: 0 }}>模型设置</h2>
    </Space>
  );
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ThunderboltOutlined />} onClick={onTest} loading={testing} disabled={loading}>
        测试连通
      </Button>
      <Button
        type="primary"
        icon={<SaveOutlined />}
        onClick={() => void onSave()}
        loading={saving}
        disabled={loading}
      >
        保存配置
      </Button>
      <AdminHeaderRight />
    </Space>
  );

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      <Form<ModelConfig>
        form={form}
        layout="vertical"
        initialValues={{ embedding_device: 'cpu', rerank_device: 'cpu' }}
        style={{ maxWidth: 900 }}
      >
        {/* LLM 生成 */}
        <Card
          title={
            <Space>
              <RobotOutlined />
              <span>大模型生成（LLM）</span>
              <Tag color="blue">问答答案由此模型生成</Tag>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item
                name="llm_provider"
                label="提供商"
                rules={[{ required: true }]}
              >
                <Select options={LLM_PROVIDER_OPTIONS} onChange={onProviderChange} />
              </Form.Item>
            </Col>
            <Col xs={24} md={16}>
              <Form.Item
                name="llm_base_url"
                label="Base URL"
                rules={[{ required: true, message: '请输入 Base URL' }]}
              >
                <Input placeholder="如 https://api.deepseek.com/v1" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item
                name="llm_model"
                label="模型名称"
                rules={[{ required: true, message: '请输入模型名称' }]}
              >
                <Input placeholder="如 deepseek-chat / gpt-4o-mini / qwen-plus" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="llm_api_key"
                label="API Key"
                extra={
                  apiKeyState.configured ? (
                    <span>
                      已配置（{apiKeyState.masked}）—— 已持久化保存，
                      <b>无需重新输入</b>；只有在要更换 Key 时才填写新值。
                    </span>
                  ) : (
                    '未配置：填写后保存即持久化，不会因重新登录而丢失。'
                  )
                }
              >
                <Input.Password
                  placeholder={
                    apiKeyState.configured
                      ? `已保存（${apiKeyState.masked}），留空表示继续使用原 Key`
                      : '输入 API Key（保存后显示为脱敏值）'
                  }
                  autoComplete="new-password"
                />
              </Form.Item>
            </Col>
          </Row>
          {testResult && (
            <Alert
              type={testResult.ok ? 'success' : 'error'}
              showIcon
              message={testResult.msg}
              style={{ marginTop: 8 }}
            />
          )}
        </Card>

        {/* Embedding 向量化 */}
        <Card
          title={
            <Space>
              <ApiOutlined />
              <span>向量化（Embedding）</span>
              <Tag color="purple">文档检索质量</Tag>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item name="embedding_backend" label="后端">
                <Select
                  options={[
                    // 模型权重随镜像内置，无需安装
                    { label: 'BGE-M3（FlagEmbedding）', value: 'flagembedding' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={10}>
              <Form.Item name="embedding_model" label="模型">
                <Input placeholder="BAAI/bge-m3" />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item name="embedding_device" label="设备">
                <Select
                  options={[
                    { label: 'CPU', value: 'cpu' },
                    { label: 'GPU (CUDA)', value: 'cuda' },
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>
          <Divider style={{ margin: '12px 0' }} />

          {/* 状态一览：当前模型 / 运行状态 / 存量向量模型 / 待重建数量 */}
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap size={8}>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                当前模型：
              </Typography.Text>
              <Tag color="blue">{embStatus?.config.model_key ?? '—'}</Tag>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                运行状态：
              </Typography.Text>
              <Tag color={dependencyColor(dependencyState)}>
                {dependencyLabel(dependencyState)}
              </Tag>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                已有向量模型：
              </Typography.Text>
              <Tag>{embStatus?.vectors.stale_models?.length ? embStatus.vectors.stale_models.join('、') : embStatus?.config.model_key ?? '—'}</Tag>
            </Space>

            {/* 存量旧模型向量：显示待重建数量 + 一键批量重建 */}
            {(embStatus?.vectors.stale_documents ?? 0) > 0 && (
              <Alert
                type="warning"
                showIcon
                message={`检测到 ${embStatus?.vectors.stale_documents} 篇文档的向量由旧模型生成，需重新向量化后才能正常检索`}
                description="旧模型向量与新模型不在同一向量空间，检索时会被自动排除；点击下方按钮可一次性后台重建。"
                action={
                  canStartRevectorize(embStatus?.vectors.stale_documents ?? 0, embStatus?.job) ? (
                    <Button size="small" type="primary" loading={embBusy} onClick={handleRevectorize}>
                      批量重新向量化
                    </Button>
                  ) : undefined
                }
              />
            )}

            {/* 任务进度：总数 / 已完成 / 处理中 / 失败数 / 进度条 */}
            {embStatus?.job && (
              <div>
                <Space wrap size={8} style={{ marginBottom: 6 }}>
                  <Tag color={shouldPollJob(jobStatus) ? 'processing' : 'default'}>
                    {jobStatusLabel(jobStatus)}
                  </Tag>
                  <Typography.Text style={{ fontSize: 13 }}>
                    {formatJobSummary(embStatus.job)}
                  </Typography.Text>
                  {embStatus.job.total > 0 && (
                    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                      处理中 {embStatus.job.processing}
                    </Typography.Text>
                  )}
                  {embStatus.job.total > 0 && (
                    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                      当前批次 {embStatus.job.current_batch ?? 0}/{embStatus.job.total_batches ?? 0}
                      （每批 {embStatus.job.batch_size ?? '—'} 篇，批量 embedding + 批量写入 Milvus）
                    </Typography.Text>
                  )}
                  {embStatus.job.failed > 0 && !shouldPollJob(jobStatus) && (
                    <Button size="small" danger loading={embBusy} onClick={handleRetryRevectorize}>
                      重试失败项（{embStatus.job.failed}）
                    </Button>
                  )}
                  {canCancelJob(jobStatus) && (
                    <Popconfirm
                      title="确认取消本次批量重新向量化？"
                      description="已完成的文档会保留新向量，剩余文档仍是旧模型向量，可稍后再次发起。"
                      okText="确认取消"
                      cancelText="继续处理"
                      onConfirm={() => void handleCancelRevectorize()}
                    >
                      <Button size="small" loading={embBusy}>
                        取消任务
                      </Button>
                    </Popconfirm>
                  )}
                </Space>
                <Progress
                  percent={embStatus.job.percent}
                  status={
                    embStatus.job.failed > 0
                      ? 'exception'
                      : embStatus.job.status === 'succeeded'
                        ? 'success'
                        : 'active'
                  }
                  size="small"
                />
                {embStatus.job.error_message && (
                  <Typography.Text type="danger" style={{ fontSize: 12 }}>
                    失败原因：{embStatus.job.error_message}
                  </Typography.Text>
                )}
              </div>
            )}

            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              切换 Embedding 模型后，系统会自动识别旧模型生成的向量：检索时排除它们以避免混检，
              并支持一键后台批量重建（无需逐篇点击「重新向量化」）。
            </Typography.Text>
          </Space>
        </Card>

        {/* Rerank 精排 */}
        <Card
          title={
            <Space>
              <ExperimentOutlined />
              <span>重排序（Rerank）</span>
              <Tag color="cyan">检索相关性精排</Tag>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item name="rerank_backend" label="后端">
                <Select
                  options={[
                    { label: 'BGE-Reranker-v2-m3（FlagEmbedding）', value: 'flagreranker' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={10}>
              <Form.Item name="rerank_model" label="模型">
                <Input placeholder="BAAI/bge-reranker-v2-m3" />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item name="rerank_device" label="设备">
                <Select
                  options={[
                    { label: 'CPU', value: 'cpu' },
                    { label: 'GPU (CUDA)', value: 'cuda' },
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        {/* HyDE 查询改写 */}
        <Card
          title={
            <Space>
              <ThunderboltOutlined />
              <span>HyDE 查询改写</span>
              <Tag>用假设答案提升检索召回</Tag>
            </Space>
          }
        >
          <Form.Item name="hyde_enabled" label="启用 HyDE" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item name="hyde_backend" label="后端">
                <Select
                  options={[{ label: '复用 LLM（OpenAI 兼容）', value: 'openai' }]}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="hyde_model" label="模型">
                <Input placeholder="留空则使用主 LLM 模型" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="hyde_base_url" label="Base URL">
                <Input placeholder="留空则复用主 LLM Base URL" />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Divider />
        <Space>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            onClick={() => void onSave()}
            loading={saving}
          >
            保存配置
          </Button>
          <Button onClick={onTest} loading={testing}>
            测试 LLM 连通
          </Button>
        </Space>
      </Form>

      {/* ── 系统级配置（BUSINESS_RULES §8）── */}
      <Card
        title={<Space><SettingOutlined /> 系统配置</Space>}
        style={{ maxWidth: 900, marginTop: 24 }}
        loading={sysLoading}
      >
        <Form
          form={sysForm}
          layout="vertical"
        >
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                name="trash_retention_days"
                label="回收站保留期（天）"
                extra={defaultHint('trash_retention_days')}
                rules={[{ required: true, message: '请输入保留天数' }, { type: 'number', min: 1, max: 30, message: '1-30 天' }]}
              >
                <InputNumber min={1} max={30} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="max_file_size_mb"
                label="文件大小限制（MB）"
                extra={defaultHint('max_file_size_mb')}
                rules={[{ required: true, message: '请输入文件大小限制' }, { type: 'number', min: 1, max: 500, message: '1-500 MB' }]}
              >
                <InputNumber min={1} max={500} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="history_window"
                label="多轮对话历史轮数"
                extra={defaultHint('history_window')}
                rules={[{ required: true, message: '请输入历史轮数' }, { type: 'number', min: 0, max: 20, message: '0-20 轮' }]}
              >
                <InputNumber min={0} max={20} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                name="recall_top_k"
                label="检索召回数量（Top-K）"
                extra={defaultHint('recall_top_k')}
                rules={[{ required: true, message: '请输入召回数量' }, { type: 'number', min: 1, max: 200 }]}
              >
                <InputNumber min={1} max={200} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="rerank_top_n"
                label="精排返回数量（Top-N）"
                extra={defaultHint('rerank_top_n')}
                rules={[{ required: true, message: '请输入精排数量' }, { type: 'number', min: 1, max: 50 }]}
              >
                <InputNumber min={1} max={50} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="relevance_threshold"
                label="相似度拒答阈值"
                extra={defaultHint('relevance_threshold')}
                rules={[{ required: true, message: '请输入阈值' }, { type: 'number', min: 0, max: 1 }]}
              >
                <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Space>
            <Button type="primary" icon={<SaveOutlined />} onClick={() => void onSysSave()} loading={sysSaving} disabled={sysLoading}>
              保存系统配置
            </Button>
            <Popconfirm
              title="恢复为默认值？"
              description="六个配置项将全部写回系统默认值（当前自定义取值会被覆盖）。"
              okText="恢复默认"
              cancelText="取消"
              onConfirm={() => void onSysReset()}
            >
              <Button icon={<ReloadOutlined />} loading={sysResetting} disabled={sysLoading}>
                恢复默认值
              </Button>
            </Popconfirm>
          </Space>
        </Form>
      </Card>
    </AppLayout>
  );
}
