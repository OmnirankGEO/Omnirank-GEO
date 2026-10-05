import { useAuth } from "@/context/AuthContext";
import { useUserMode } from '@/context/UserModeContext';
import { AdminDashboard } from '@/pages/Home/AdminDashboard';
import { GeoHome } from '@/pages/Home/GeoHome';
import { FullHome } from '@/pages/Home/FullHome';

// ========== 首页路由分发 ==========
// 管理员 → AdminDashboard(运营控制台)
// GEO 用户 → GeoHome(品牌工作台)
// 全量用户 → FullHome(双栏工作台)
//
// 🔴 [WO_260 · 2026-09-23] 两处改动:
//   ① 社媒用户原先 `<Navigate to="/s" replace />` —— 社媒随 E3 删域,那条路由删了就是 404。
//      改为与全量用户同落 FullHome(UserModeContext 的缺省值本来就是 'full')。
//   ② 原第 68–601 行整段是**不可达代码**:UserMode 只有 'geo' | 'social' | 'full' 三个值
//      (context 读 localStorage 时不在这三值里就回落 'full'),上面几个分支必有一个 return;
//      OSS_01 J 章 §1 也独立把这一段标为死区。里面的「社媒操盘手 / 知识库管理 / 顾问团队 /
//      AI 员工会议」入口卡与 /s /employees /knowledge 链接随死区一并删 —— 运行时行为不变。
export function Dashboard() {
    const { user } = useAuth();
    const { mode } = useUserMode();

    if (user?.is_admin) {
        return <AdminDashboard />;
    }
    if (mode === 'geo') {
        return <GeoHome />;
    }
    return <FullHome />;
}
