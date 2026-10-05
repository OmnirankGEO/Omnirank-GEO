import { useState, useEffect, useRef } from 'react';

interface ThinkingStep {
  text: string;
  done: boolean;
}

interface UseSimulatedThinkingOptions {
  steps: string[];
  intervalMs?: number;
}

export function useSimulatedThinking(
  isLoading: boolean,
  options: UseSimulatedThinkingOptions
) {
  const { steps, intervalMs = 800 } = options;
  const [currentStepIndex, setCurrentStepIndex] = useState(-1);
  const [thinkingSteps, setThinkingSteps] = useState<ThinkingStep[]>([]);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!isLoading) {
      if (thinkingSteps.length > 0) {
        setThinkingSteps(prev => prev.map(s => ({ ...s, done: true })));
        const timeout = setTimeout(() => {
          setThinkingSteps([]);
          setCurrentStepIndex(-1);
        }, 1000);
        return () => clearTimeout(timeout);
      }
      return;
    }

    setCurrentStepIndex(0);
    setThinkingSteps([{ text: steps[0], done: false }]);

    let i = 1;
    timerRef.current = setInterval(() => {
      if (i < steps.length) {
        setThinkingSteps(prev => [
          ...prev.map(s => ({ ...s, done: true })),
          { text: steps[i], done: false },
        ]);
        setCurrentStepIndex(i);
        i++;
      } else {
        if (timerRef.current) clearInterval(timerRef.current);
      }
    }, intervalMs);

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [isLoading]);

  return {
    thinkingSteps,
    isThinking: isLoading && thinkingSteps.length > 0,
    currentStepIndex,
  };
}
