'use client';

import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Form,
  Input,
  InputNumber,
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
  RobotOutlined,
  SaveOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import type { ModelConfig } from '@/services/api';
import { fetchModelConfig, testModelConnection, updateModelConfig, fetchSystemConfig, updateSystemConfig } from '@/services/api';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';

const LLM_PROVIDER_OPTIONS = [
  { label: 'Mock（假回答，开发调试）', value: 'mock' },
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

  const loadConfig = async () => {
    setLoading(true);
    try {
      const cfg = await fetchModelConfig();
      form.setFieldsValue(cfg);
    } catch {
      message.error('加载模型配置失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadConfig();
    void loadSysConfig();
  }, []);

  const loadSysConfig = async () => {
    setSysLoading(true);
    try {
      const cfg = await fetchSystemConfig();
      sysForm.setFieldsValue(cfg);
    } catch {
      message.error('加载系统配置失败');
    } finally {
      setSysLoading(false);
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

  /** 从 axios 错误中提取后端返回的可读信息。 */
  const describeError = (err: unknown): string => {
    const e = err as { response?: { data?: { detail?: unknown } }; message?: string };
    const detail = e?.response?.data?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (e?.message) return e.message;
    return '请求失败，请检查后端服务是否正常';
  };

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

  const llmProvider = Form.useWatch('llm_provider', form);

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
              <Form.Item name="llm_api_key" label="API Key">
                <Input.Password
                  placeholder="输入 API Key（保存后显示为脱敏值）"
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
          {llmProvider === 'mock' && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 8 }}
              message="当前为 Mock 模式"
              description="将返回占位假回答，不会调用真实模型。请选择提供商并填写 API Key 后测试连通。"
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
                    { label: 'Mock（开发调试）', value: 'mock' },
                    { label: 'BGE-M3（FlagEmbedding，需安装）', value: 'flagembedding' },
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
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            切换为 BGE-M3 后，已入库文档需点击「重新向量化」才能正常检索。FlagEmbedding 需自行安装：
            <code>pip install FlagEmbedding</code>
          </Typography.Text>
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
                    { label: 'Mock（开发调试）', value: 'mock' },
                    { label: 'BGE-Reranker-v2-m3（需安装 FlagEmbedding）', value: 'flagreranker' },
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
                  options={[
                    { label: 'Mock（开发调试）', value: 'mock' },
                    { label: '复用 LLM（OpenAI 兼容）', value: 'openai' },
                  ]}
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
                rules={[{ required: true, message: '请输入保留天数' }, { type: 'number', min: 1, max: 30, message: '1-30 天' }]}
              >
                <InputNumber min={1} max={30} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="max_file_size_mb"
                label="文件大小限制（MB）"
                rules={[{ required: true, message: '请输入文件大小限制' }, { type: 'number', min: 1, max: 500, message: '1-500 MB' }]}
              >
                <InputNumber min={1} max={500} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="history_window"
                label="多轮对话历史轮数"
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
                rules={[{ required: true, message: '请输入召回数量' }, { type: 'number', min: 1, max: 200 }]}
              >
                <InputNumber min={1} max={200} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="rerank_top_n"
                label="精排返回数量（Top-N）"
                rules={[{ required: true, message: '请输入精排数量' }, { type: 'number', min: 1, max: 50 }]}
              >
                <InputNumber min={1} max={50} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="relevance_threshold"
                label="相似度拒答阈值"
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
          </Space>
        </Form>
      </Card>
    </AppLayout>
  );
}
