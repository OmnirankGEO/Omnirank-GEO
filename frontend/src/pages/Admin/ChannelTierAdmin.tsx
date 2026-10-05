import { ChannelTierPanel } from './ChannelTierPanel';

/** 旧路由兼容页；渠道奖励业务只维护在共享面板中。 */
export default function ChannelTierAdmin() {
  return (
    <>
      <p className="sr-only">实际到账算力由实时进货折扣和渠道等级自动计算</p>
      <ChannelTierPanel />
    </>
  );
}
