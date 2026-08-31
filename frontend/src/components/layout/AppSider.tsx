'use client';

import { Button, Divider, Layout, List, Spin, Typography } from 'antd';
import {
  CheckCircleFilled,
  DatabaseOutlined,
  HistoryOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import { useChatStore } from '@/stores/chatStore';

const { Sider } = Layout;

/** 左侧边栏：知识库列表 + 历史会话（PRD 5.1，ChatGPT 风格）。 */
export function AppSider() {
  const {
    knowledgeBases,
    selectedKbId,
    selectKb,
    conversations,
    currentConversationId,
    selectConversation,
    newConversation,
    loadingKbs,
    loadingConversations,
    loadConversations,
  } = useChatStore();

  return (
    <Sider
      width={260}
      theme="light"
      style={{ borderRight: '1px solid #f0f0f0', padding: 16, overflow: 'auto', height: '100vh', position: 'sticky', top: 0 }}
    >
      <Button
        type="dashed"
        icon={<PlusOutlined />}
        block
        onClick={() => newConversation()}
      >
        新建会话
      </Button>

      <Divider orientation="left" plain>
        <DatabaseOutlined /> 知识库
      </Divider>
      <Spin spinning={loadingKbs} size="small">
        <List
          size="small"
          dataSource={knowledgeBases}
          locale={{ emptyText: '暂无知识库' }}
          renderItem={(kb) => {
            const active = kb.id === selectedKbId;
            return (
              <List.Item
                style={{
                  cursor: 'pointer',
                  background: active ? '#e6f4ff' : 'transparent',
                  borderRadius: 6,
                  paddingInline: 8,
                }}
                onClick={() => selectKb(kb.id)}
              >
                <Typography.Text ellipsis style={{ maxWidth: 130, fontWeight: active ? 600 : 400 }}>
                  {kb.name}
                </Typography.Text>
                {active && <CheckCircleFilled style={{ color: '#1677ff', marginLeft: 4 }} />}
              </List.Item>
            );
          }}
        />
      </Spin>

      <Divider orientation="left" plain>
        <HistoryOutlined /> 历史会话
      </Divider>
      <Spin spinning={loadingConversations} size="small">
        <List
          size="small"
          dataSource={conversations}
          locale={{ emptyText: '暂无历史会话' }}
          renderItem={(conv) => {
            const active = conv.id === currentConversationId;
            return (
              <List.Item
                style={{
                  cursor: 'pointer',
                  background: active ? '#e6f4ff' : 'transparent',
                  borderRadius: 6,
                  paddingInline: 8,
                }}
                onClick={() => selectConversation(conv.id)}
              >
                <Typography.Text ellipsis style={{ maxWidth: 180, fontWeight: active ? 600 : 400 }}>
                  {conv.title}
                </Typography.Text>
              </List.Item>
            );
          }}
        />
      </Spin>

      <div style={{ marginTop: 12 }}>
        <Button type="link" size="small" onClick={() => loadConversations()} style={{ padding: 0 }}>
          刷新会话列表
        </Button>
      </div>
    </Sider>
  );
}
