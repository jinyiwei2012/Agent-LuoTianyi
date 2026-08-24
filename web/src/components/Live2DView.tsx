import { useEffect, useImperativeHandle, useRef, useState, forwardRef } from 'react';
import { loadOml2d } from 'oh-my-live2d';
import type { Oml2dEvents, Oml2dMethods, Oml2dProperties } from 'oh-my-live2d';
import { LIVE2D_CONFIG } from '@/config/live2d';
import { addDebugTrace } from '@/utils/debug_trace';

type Oml2dInstance = Oml2dProperties & Oml2dMethods & Oml2dEvents;

export interface Live2DViewHandle {
  setExpression: (expression: string) => void;
}

export interface Live2DViewProps {
  expression?: string;
  className?: string;
}

type Live2DModel = {
  expression: (name: string) => Promise<boolean>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function getLive2dModel(instance: Oml2dInstance): Live2DModel | null {
  if (!isRecord(instance)) {
    return null;
  }
  const models = instance['models'];
  if (!isRecord(models)) {
    return null;
  }
  const model = models['model'];
  if (!isRecord(model)) {
    return null;
  }
  const expression = model['expression'];
  if (typeof expression !== 'function') {
    return null;
  }
  return {
    expression: expression.bind(model) as Live2DModel['expression'],
  };
}

function getExpressionName(expression: string): string {
  const projection: Record<string, string> = LIVE2D_CONFIG.expression_projection;
  return projection[expression] ?? expression;
}

function getModelPath(): string {
  return `${import.meta.env.BASE_URL}live2d/models/luo/model.model3.json`;
}

export const Live2DView = forwardRef<Live2DViewHandle, Live2DViewProps>(function Live2DView(
  { expression, className = '' },
  ref,
) {
  const hostRef = useRef<HTMLDivElement>(null);
  const runtimeRef = useRef<Oml2dInstance | null>(null);
  const modelRef = useRef<Live2DModel | null>(null);
  const pendingExpressionRef = useRef<string | null>(expression ?? null);
  const [loadState, setLoadState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [activeExpression, setActiveExpression] = useState(expression ?? '微笑脸');

  const setExpression = (nextExpression: string) => {
    pendingExpressionRef.current = nextExpression;
    setActiveExpression(nextExpression);
    const model = modelRef.current;
    if (!model) {
      return;
    }
    const modelExpression = getExpressionName(nextExpression);
    void model.expression(modelExpression).then((success) => {
      if (!success) {
        addDebugTrace('live2d', 'expression unavailable', { expression: modelExpression });
      }
    }).catch((error: unknown) => {
      addDebugTrace('live2d', 'expression failed', { error: String(error), expression: modelExpression });
    });
  };

  useImperativeHandle(ref, () => ({ setExpression }), []);

  useEffect(() => {
    pendingExpressionRef.current = expression ?? null;
    if (expression) {
      setExpression(expression);
    }
  }, [expression]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) {
      return;
    }

    let active = true;
    let runtime: Oml2dInstance;
    try {
      runtime = loadOml2d({
      parentElement: host,
      dockedPosition: 'left',
      mobileDisplay: true,
      primaryColor: 'var(--color-accent)',
      sayHello: false,
      transitionTime: 420,
      initialStatus: 'active',
      stageStyle: {
        width: '100%',
        height: '100%',
        position: 'absolute',
        left: 0,
        bottom: 0,
        zIndex: 1,
        transform: 'none',
      },
      models: [
        {
          name: 'luo-tianyi',
          path: getModelPath(),
          scale: 0.16,
          mobileScale: 0.12,
          position: [0, 12],
          mobilePosition: [0, 10],
          stageStyle: {
            width: '100%',
            height: '100%',
            position: 'absolute',
            left: 0,
            bottom: 0,
            zIndex: 1,
            transform: 'none',
          },
          mobileStageStyle: {
            width: '100%',
            height: '100%',
            position: 'absolute',
            left: 0,
            bottom: 0,
            zIndex: 1,
            transform: 'none',
          },
          motionPreloadStrategy: 'IDLE',
          volume: 0,
        },
      ],
      statusBar: { disable: true },
      menus: { disable: true },
      tips: {
        idleTips: { message: [], duration: 0, interval: 100000 },
        welcomeTips: { message: {}, duration: 0 },
        copyTips: { message: [], duration: 0 },
      },
      });
    } catch (error: unknown) {
      setLoadState('error');
      addDebugTrace('live2d', 'model initialization failed', { error: String(error) });
      return;
    }
    runtimeRef.current = runtime;

    runtime.onLoad((status) => {
      if (!active) {
        return;
      }
      if (status === 'success') {
        setLoadState('ready');
        modelRef.current = getLive2dModel(runtime);
        const pending = pendingExpressionRef.current;
        if (pending) {
          setExpression(pending);
        }
        addDebugTrace('live2d', 'model loaded');
      } else if (status === 'fail') {
        setLoadState('error');
        addDebugTrace('live2d', 'model load failed');
      }
    });

    return () => {
      active = false;
      modelRef.current = null;
      runtimeRef.current = null;
      host.replaceChildren();
    };
  }, []);

  return (
    <section className={`live2d-view ${className}`} aria-label="洛天依 Live2D 舞台">
      <div className="live2d-view__glow" aria-hidden="true" />
      <div ref={hostRef} className="live2d-view__host" aria-hidden="true" />
      <div className="live2d-view__caption">
        <span className="live2d-view__status-dot" data-state={loadState} aria-hidden="true" />
        <span>{loadState === 'ready' ? '天依在线' : loadState === 'error' ? '模型暂不可用' : '正在唤醒天依'}</span>
        <span className="live2d-view__expression">{activeExpression}</span>
      </div>
    </section>
  );
});
