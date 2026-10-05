/**
 * Fixture-free production candidate probe.
 * 只验证 feature index + HTTP transport 的生产可达 bundle；不接 App/SharedReport。
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../../../index.css';
import { PublicReportPage, createHttpTransport } from '../index';

const params = new URLSearchParams(window.location.search);
const reportId = params.get('id') ?? '1';
const shareToken = params.get('st');
const root = document.getElementById('root');

if (!root) throw new Error('production candidate probe: #root missing');

createRoot(root).render(
    <StrictMode>
        <PublicReportPage
            transport={createHttpTransport()}
            reportId={reportId}
            shareToken={shareToken}
        />
    </StrictMode>,
);
