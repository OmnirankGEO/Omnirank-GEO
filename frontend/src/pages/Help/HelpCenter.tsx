// 帮助中心视频主页。
// /help 继续直达角色说明书；本页只通过 /help/home 提供视频学习与教程重走入口。

import { Link } from 'react-router-dom'
import {
  Activity,
  ArrowRight,
  BookOpenCheck,
  Clock,
  FileSpreadsheet,
  HelpCircle,
  PenLine,
  PlayCircle,
  Sparkles,
  Stethoscope,
} from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { VideoCard, VideoThumb } from '@/components/help/VideoCard'
import { videos } from '@/components/help/videos-data'
import { useAuth } from '@/context/AuthContext'
import { useBranding } from '@/hooks/useWhitelabel'
import { cn } from '@/lib/utils'
import { enterSandbox, resetSandboxTutorialState } from '@/sandbox/sandboxState'

type VideoStep = 1 | 2 | 3 | 4

type LearningStep = {
  order: number
  videoStep: VideoStep
  title: string
  desc: string
  icon: typeof Stethoscope
  accent: string
}

const providerSteps: LearningStep[] = [
  {
    order: 1,
    videoStep: 1,
    title: '做品牌诊断',
    desc: '看清品牌在 AI 搜索中的现状，生成便于讲解的报告。',
    icon: Stethoscope,
    accent: 'bg-blue-500/10 text-blue-500',
  },
  {
    order: 2,
    videoStep: 2,
    title: '出报价方案',
    desc: '讲清推广方向、关键词和价格，在线完成方案确认。',
    icon: FileSpreadsheet,
    accent: 'bg-violet-500/10 text-violet-500',
  },
  {
    order: 3,
    videoStep: 3,
    title: 'AI 写文章并发布',
    desc: '生成内容、检查成稿、选择渠道并跟踪发布状态。',
    icon: PenLine,
    accent: 'bg-emerald-500/10 text-emerald-500',
  },
  {
    order: 4,
    videoStep: 4,
    title: '持续监测效果',
    desc: '查看推荐变化、处理未达标词条并完成效果交付。',
    icon: Activity,
    accent: 'bg-amber-500/10 text-amber-500',
  },
]

const normalUserSteps: LearningStep[] = [
  {
    order: 1,
    videoStep: 3,
    title: 'AI 写文章并发布',
    desc: '从写作任务开始，检查成稿并选择合适渠道发布。',
    icon: PenLine,
    accent: 'bg-emerald-500/10 text-emerald-500',
  },
  {
    order: 2,
    videoStep: 4,
    title: '查看内容效果',
    desc: '查看词条表现、变化趋势和下一步优化建议。',
    icon: Activity,
    accent: 'bg-amber-500/10 text-amber-500',
  },
]

export default function HelpCenter() {
  const { user } = useAuth()
  /* 🔴 [#222 a1b'] 这里原有 isAdmin / authLevel / isAgent 三个变量,只服务于本页的内容分层。
     分层已拉平(视频清单 / 学习路径 / 文案全部走服务商那一份),三个变量随之作废。
     经营后台那一层不在本页,在 docs-data.ts 的「经营后台」一节,由 HelpDocs 的 audience 机制继续挡着。
     留着没人读的身份变量,下一个人会以为本页仍按身份分流。 */
  const { brand: helpBrand, backofficeBrandAllowed } = useBranding({ userId: user?.id })
  const productName =
    (backofficeBrandAllowed ? helpBrand?.product_name?.trim() : '') ||
    '全域上榜GEO交付系统'

  /*
   * 🔴 [#222 a1b' 2026-09-16] 帮助中心内容分层**拉平**(Owner:普通账号 = 服务商,除经营后台)。
   *
   *    枚举过才改,不按「24 行 / 50 处」这类计数动手:
   *    `videos-data.ts` 里 `audience: 'agent'` 共 7 条,**全是主流程交付内容**
   *    (品牌诊断 / 看懂报告 / 在线报价 / AI 写文章 / 开始监测 / 白标链接+月报 / 全流程一镜到底)——
   *    没有一条是经营后台(进货价 / 佣金 / 结算)。普通账号本来就能用这些功能,
   *    却只看得到 15 个视频里的 8 个 —— 恰好缺的是教他用这些功能的那几个。
   *    学习路径同理:普通账号只有 2 步(写文章 / 看效果),服务商 4 步(诊断→报价→写作→监测)。
   *
   * 🔴 经营后台那一层**不在这里**,在 `docs-data.ts` 的「经营后台」一节(`audience: 'agent'`),
   *    由 `HelpDocs.tsx` 的 audience 机制继续挡着 —— 那套**不动**,动了就把排除项一起放开了。
   *
   * 🔴 代价(交付单已记,请 Owner 过目):文案仍是服务商口径,
   *    普通账号现在会看到「服务商新手必看」「按工作流学习」这类措辞。
   *    这是内容去身份化(乙案)要解决的,不在本单。
   */
  const visibleVideos = videos
  const hero = visibleVideos.find((video) => video.id === 'V5')
  const learningSteps = providerSteps
  const concepts = visibleVideos.filter((video) => video.category === 'concept')
  const videosByStep = (step: VideoStep) =>
    visibleVideos.filter((video) => video.category === 'step' && video.step === step)

  const handleRestartTutorial = () => {
    resetSandboxTutorialState()
    enterSandbox()
    toast.success('已进入教程模式，欢迎引导马上出现')
    window.setTimeout(() => window.location.reload(), 300)
  }

  return (
    <div className="min-h-full bg-background">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-10 px-4 py-8 md:px-8 md:py-10">
        <header className="flex flex-col gap-3 border-b pb-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-xs font-medium text-primary">帮助中心</p>
            <h1 className="mt-1 text-xl font-semibold text-foreground">视频教程主页</h1>
          </div>
          <Button asChild variant="outline" className="min-h-11 w-full sm:w-auto">
            <Link to="/help">
              <BookOpenCheck className="size-4" />
              打开操作说明书
              <ArrowRight className="size-3.5" />
            </Link>
          </Button>
        </header>

        {/* [#222 a1b'] 新手引导重开入口:a1b 已把两个引导弹窗对全员放开,这里跟上,否则重开不了 */}
        {(
          <section
            data-slot="onboarding-restart-placeholder"
            className="flex flex-col gap-4 rounded-lg border bg-card px-5 py-5 sm:flex-row sm:items-center sm:justify-between md:px-6"
          >
            <div className="flex items-start gap-3">
              <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
                <PlayCircle className="size-5" />
              </span>
              <div>
                <h2 className="text-base font-semibold text-foreground">重走新手教程</h2>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  清空上次教程进度，从品牌诊断开始重新走完服务商工作流程。教程数据不会影响真实业务。
                </p>
              </div>
            </div>
            <Button
              type="button"
              size="lg"
              onClick={handleRestartTutorial}
              data-help-cta="restart-tutorial"
              className="min-h-11 w-full bg-emerald-400 px-6 font-semibold text-emerald-950 shadow-[0_0_0_1px_rgba(52,211,153,0.35),0_0_22px_rgba(52,211,153,0.28)] transition-[transform,background-color,box-shadow] duration-200 hover:-translate-y-0.5 hover:bg-emerald-300 hover:text-emerald-950 hover:shadow-[0_0_0_1px_rgba(110,231,183,0.5),0_0_28px_rgba(52,211,153,0.38)] focus-visible:ring-2 focus-visible:ring-emerald-300 motion-safe:animate-[pulse-subtle_2.6s_ease-in-out_infinite] motion-reduce:animate-none sm:w-auto"
            >
              <PlayCircle className="size-4" />
              开始教程
            </Button>
          </section>
        )}

        {hero && (
          <section className="grid gap-6 md:grid-cols-[1.25fr_1fr] md:items-center">
            <div>
              <span className="mb-3 inline-flex items-center gap-2 rounded-full bg-primary/10 px-3 py-1 text-xs font-medium text-primary">
                <Sparkles className="size-3.5" />
                服务商新手必看
              </span>
              <h2 className="text-3xl font-semibold text-foreground md:text-4xl">
                {`5 分钟看懂${productName}怎么用`}
              </h2>
              <p className="mt-3 max-w-xl text-sm leading-7 text-muted-foreground md:text-base">
                一个视频走完品牌诊断、报价方案、内容生产和效果监测，按实际工作顺序快速上手。
              </p>
              <div className="mt-4 flex items-center gap-2 text-xs text-muted-foreground">
                <Clock className="size-3.5" />
                {hero.duration && <span>{hero.duration}</span>}
                <span>·</span>
                <span>服务商全流程演示</span>
              </div>
            </div>
            <VideoThumb video={hero} large />
          </section>
        )}

        <section>
          <header className="mb-5">
            <h2 className="text-xl font-semibold text-foreground">
              按工作流学习
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              按服务商日常交付的顺序学习，每一步都能直接找到对应视频。
            </p>
          </header>

          <div
            className={cn(
              'mb-8 grid gap-3',
              /* 🔴 学习路径现在恒为 providerSteps(4 步),栅格必须跟着定成 4 列 ——
                 只改数据不改布局的话,4 张卡会挤进 2 列,是拉平顺带带出来的错版 */
              'md:grid-cols-4',
            )}
          >
            {learningSteps.map((step) => {
              const count = videosByStep(step.videoStep).length
              return (
                <a
                  key={step.videoStep}
                  href={`#step-${step.videoStep}`}
                  className="group rounded-lg border bg-card p-4 transition-colors hover:border-foreground/30 hover:bg-muted/30"
                >
                  <div className="mb-3 flex items-center justify-between">
                    <span className={cn('grid size-9 place-items-center rounded-lg', step.accent)}>
                      <step.icon className="size-4" />
                    </span>
                    <span className="text-xs text-muted-foreground">第 {step.order} 步</span>
                  </div>
                  <h3 className="text-sm font-semibold text-foreground">{step.title}</h3>
                  <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{step.desc}</p>
                  <p className="mt-3 text-xs font-medium text-muted-foreground">{count} 个视频</p>
                </a>
              )
            })}
          </div>

          <div className="space-y-10">
            {learningSteps.map((step) => {
              const list = videosByStep(step.videoStep)
              return (
                <div
                  key={step.videoStep}
                  id={`step-${step.videoStep}`}
                  className="scroll-mt-6"
                >
                  <h3 className="mb-4 flex items-center gap-2.5 text-base font-semibold text-foreground">
                    <span
                      className={cn(
                        'grid size-7 place-items-center rounded-md text-xs font-bold',
                        step.accent,
                      )}
                    >
                      {step.order}
                    </span>
                    {step.title}
                    <span className="text-xs font-normal text-muted-foreground">
                      · {list.length} 个视频
                    </span>
                  </h3>
                  <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
                    {list.map((video) => (
                      <VideoCard key={video.id} video={video} />
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        </section>

        <section>
          <header className="mb-5">
            <h2 className="text-xl font-semibold text-foreground">GEO 概念说明</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              用短视频了解 GEO、评分、效果周期和功能消耗。
            </p>
          </header>
          <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
            {concepts.map((video) => (
              <VideoCard key={video.id} video={video} />
            ))}
          </div>
        </section>

        <section className="border-t pt-8">
          <h2 className="text-lg font-semibold text-foreground">继续查找具体操作</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            视频负责快速上手，操作说明书提供每个页面的完整步骤。
          </p>
          <div className="mt-4 flex flex-wrap gap-3">
            <Button asChild variant="outline" className="min-h-11">
              <Link to="/help">
                <BookOpenCheck className="size-4" />
                查操作说明书
                <ArrowRight className="size-3.5" />
              </Link>
            </Button>
            <Button asChild variant="outline" className="min-h-11">
              <Link to="/help/faq">
                <HelpCircle className="size-4" />
                查看常见问题
                <ArrowRight className="size-3.5" />
              </Link>
            </Button>
          </div>
        </section>
      </div>
    </div>
  )
}
