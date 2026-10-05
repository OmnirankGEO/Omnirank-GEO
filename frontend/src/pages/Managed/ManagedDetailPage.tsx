/**
 * 套餐详情页 — /managed/:id
 *
 * 含：CampaignDashboard + PendingReviewQueue
 */

import { useParams, useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { ArrowLeft } from 'lucide-react';
import { CampaignDashboard, PendingReviewQueue } from '@/components/managed';

export default function ManagedDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const campaignId = parseInt(id || '0');

  if (!campaignId) {
    return <div className="p-6 text-center text-muted-foreground">参数错误</div>;
  }

  return (
    <div className="container max-w-4xl mx-auto py-6 space-y-6">
      <Button variant="ghost" size="sm" onClick={() => navigate('/managed')}>
        <ArrowLeft className="h-4 w-4 mr-1" /> 返回列表
      </Button>

      <CampaignDashboard campaignId={campaignId} />

      <div>
        <h2 className="text-lg font-semibold mb-3">待审文章</h2>
        <PendingReviewQueue campaignId={campaignId} />
      </div>
    </div>
  );
}
