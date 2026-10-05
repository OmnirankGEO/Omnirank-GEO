import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@/index.css';
import { PreviewApp } from './PreviewApp';

const el = document.getElementById('root');
if (el) {
  createRoot(el).render(
    <StrictMode>
      <PreviewApp />
    </StrictMode>,
  );
}
