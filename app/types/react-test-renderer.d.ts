/**
 * react-test-renderer 最小类型声明（React 19 不再内置测试渲染器类型）。
 * 仅覆盖本仓库测试用到的 API 面。
 */
declare module 'react-test-renderer' {
  import type * as React from 'react';

  export interface ReactTestInstance {
    type: any;
    props: Record<string, any>;
    findAllByType(type: any): ReactTestInstance[];
    findAll(predicate: (node: ReactTestInstance) => boolean): ReactTestInstance[];
  }

  export interface ReactTestRenderer {
    root: ReactTestInstance;
  }

  export function create(element: React.ReactElement): ReactTestRenderer;
  export function act(callback: () => void | Promise<void>): Promise<void>;

  const renderer: {
    create: typeof create;
    act: typeof act;
  };
  export default renderer;
}
