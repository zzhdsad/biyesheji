'use client';

import { Empty, Tooltip } from 'antd';
import { useMemo } from 'react';

export interface ErTable {
  name: string;
  fields: string[];
  /** 主键字段名（高亮显示） */
  primaryKey?: string;
}

export interface ErRelation {
  from: string;
  to: string;
  fromField: string;
  toField: string;
  label: string;
  /** 1:1 / 1:N / M:N */
  cardinality: string;
}

interface Props {
  tables: ErTable[];
  relations: ErRelation[];
  height?: number;
}

const BOX_WIDTH = 180;
const HEADER_HEIGHT = 26;
const FIELD_HEIGHT = 18;
const BOX_PADDING = 8;
const GAP_X = 70;
const GAP_Y = 34;
const COLUMNS = 3;
const CARDINALITY_COLOR: Record<string, string> = {
  '1:1': '#52c41a',
  '1:N': '#1677ff',
  'M:N': '#fa8c16',
};

/**
 * 零依赖 SVG ER 图（决策 5：不引入 antv/g6 等图库）。
 *
 * 布局为固定列数的网格，实体框按字段数自适应高度，
 * 关系用三次贝塞尔曲线连接两框相邻边，并用箭头标记与基数着色区分关系类型。
 */
export default function ErDiagram({ tables, relations, height = 420 }: Props) {
  const layout = useMemo(() => {
    const boxes = tables.map((table, index) => {
      const column = index % COLUMNS;
      const row = Math.floor(index / COLUMNS);
      const fieldCount = Math.max(table.fields.length, 1);
      const boxHeight = HEADER_HEIGHT + fieldCount * FIELD_HEIGHT + BOX_PADDING * 2;
      const x = column * (BOX_WIDTH + GAP_X);
      const y = row * 160 + 12;
      return { table, x, y, width: BOX_WIDTH, height: boxHeight, fieldCount };
    });

    const boxByName = new Map(boxes.map((box) => [box.table.name, box]));
    const edges = relations.flatMap((relation) => {
      const source = boxByName.get(relation.from);
      const target = boxByName.get(relation.to);
      if (!source || !target) return [];
      const sourceIndex =
        source.table.fields.indexOf(relation.fromField) >= 0
          ? source.table.fields.indexOf(relation.fromField)
          : 0;
      const targetIndex =
        target.table.fields.indexOf(relation.toField) >= 0
          ? target.table.fields.indexOf(relation.toField)
          : 0;
      return [
        {
          relation,
          x1: source.x + source.width,
          y1: source.y + HEADER_HEIGHT + sourceIndex * FIELD_HEIGHT + FIELD_HEIGHT / 2,
          x2: target.x,
          y2: target.y + HEADER_HEIGHT + targetIndex * FIELD_HEIGHT + FIELD_HEIGHT / 2,
        },
      ];
    });

    const width = Math.max(
      COLUMNS * BOX_WIDTH + (COLUMNS - 1) * GAP_X + 20,
      320,
    );
    const rows = Math.ceil(tables.length / COLUMNS) || 1;
    const svgHeight = Math.max(height, rows * 160 + 20);
    return { boxes, edges, width, height: svgHeight };
  }, [tables, relations, height]);

  if (tables.length === 0) {
    return <Empty description="暂无可绘制的表结构" />;
  }

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${layout.width} ${layout.height}`}
      style={{ maxHeight: layout.height }}
      role="img"
      aria-label="实体关系图"
    >
      <defs>
        <marker
          id="er-arrow"
          markerWidth="8"
          markerHeight="8"
          refX="7"
          refY="3"
          orient="auto"
        >
          <path d="M0,0 L7,3 L0,6 Z" fill="#8c8c8c" />
        </marker>
      </defs>

      {layout.edges.map((edge, index) => {
        const color = CARDINALITY_COLOR[edge.relation.cardinality] ?? '#8c8c8c';
        const midX = (edge.x1 + edge.x2) / 2;
        return (
          <g key={`${edge.relation.from}-${edge.relation.to}-${index}`}>
            <path
              d={`M ${edge.x1} ${edge.y1} C ${midX} ${edge.y1}, ${midX} ${edge.y2}, ${edge.x2} ${edge.y2}`}
              fill="none"
              stroke={color}
              strokeWidth={1.5}
              markerEnd="url(#er-arrow)"
            />
            <Tooltip title={`${edge.relation.label}（${edge.relation.cardinality}）`}>
              <text x={midX} y={(edge.y1 + edge.y2) / 2 - 4} fontSize="10" fill={color} textAnchor="middle">
                {edge.relation.cardinality}
              </text>
            </Tooltip>
          </g>
        );
      })}

      {layout.boxes.map((box) => (
        <g key={box.table.name}>
          <rect
            x={box.x}
            y={box.y}
            width={box.width}
            height={box.height}
            rx="4"
            fill="#ffffff"
            stroke="#d9d9d9"
          />
          <rect
            x={box.x}
            y={box.y}
            width={box.width}
            height={HEADER_HEIGHT}
            rx="4"
            fill="#f0f5ff"
            stroke="#d9d9d9"
          />
          <text
            x={box.x + 8}
            y={box.y + 17}
            fontSize="12"
            fontWeight="600"
            fill="#1f1f1f"
          >
            {box.table.name.length > 20
              ? `${box.table.name.slice(0, 20)}…`
              : box.table.name}
          </text>
          {box.table.fields.map((field, index) => {
            const isPk = field === box.table.primaryKey;
            return (
              <text
                key={field}
                x={box.x + 8}
                y={box.y + HEADER_HEIGHT + BOX_PADDING + index * FIELD_HEIGHT + 12}
                fontSize="10"
                fill={isPk ? '#d46b08' : '#595959'}
                fontWeight={isPk ? 600 : 400}
              >
                {isPk ? '🔑 ' : '· '}
                {field.length > 22 ? `${field.slice(0, 22)}…` : field}
              </text>
            );
          })}
        </g>
      ))}
    </svg>
  );
}
