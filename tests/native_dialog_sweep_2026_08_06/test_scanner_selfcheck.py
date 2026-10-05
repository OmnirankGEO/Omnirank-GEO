"""[工单 2026-08-06 §1.6] 扫描器**自己**的判别力自证。

🔴 存在的理由:这一轮扫描器坏过两次,而且两次都是「报绿但 bug 还在」:

  ① 第一版不认**正则字面量**。`WritingHall.tsx:516` 有
       anchor.replace(/[#*`>\\[\\]()|_~-]/g, '')
     正则里带一个反引号 → 剥离器当成模板串开头 → 一口吞掉 50 行 →
     3402 行那个真的裸 `confirm(` 静默消失。

  ② 补上正则识别之后,`</Card>` 和 `<X ... />` 里的 `/` 又被当成正则开头 →
     `OrganizationCenter.tsx` 的两处 `window.confirm` 被吞。

  而当时我做的「逐条对账」之所以没发现,是因为**对账的两边用的是同一个剥离器** ——
  自造恒真。所以这里的样本全部是**手写期望值**,不依赖扫描器自己的任何中间产物。

每条样本都成对:一个「必须命中」配一个「必须不命中」。
"""
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCANNER = _ROOT / "scripts" / "scan_native_dialogs_2026_08_06.py"


def _scanner():
    spec = importlib.util.spec_from_file_location("scan_native_dialogs", _SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scan_text(tmp_path, source: str, name: str = "Probe.tsx"):
    """把一段源码写进临时目录,按真实入口扫描,返回命中列表。"""
    m = _scanner()
    d = tmp_path / "src"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(source, encoding="utf-8")
    return m.scan(d)


# ---------------------------------------------------------------- 必须命中
MUST_HIT = [
    ("裸 confirm —— 2026-07-01 整批漏网的那一类",
     "export function A(){ if (!confirm('删掉?')) return; }"),
    ("裸 alert",
     "export function A(){ alert('失败'); }"),
    ("window.confirm",
     "export function A(){ if (!window.confirm('删掉?')) return; }"),
    ("window.alert",
     "export function A(){ window.alert('失败'); }"),
    ("🔴 正则字面量里带反引号(WritingHall.tsx:516 的真实形态)之后的调用点",
     "export function A(s: string){\n"
     "  const core = s.replace(/[#*`>\\[\\]()|_~-]/g, '');\n"
     "  if (!confirm('删掉?')) return core;\n"
     "}"),
    ("🔴 JSX 闭合标签 </Card> 之后的调用点(OrganizationCenter 的真实形态)",
     "export function A(){\n"
     "  return <div><Card>x</Card><button onClick={() => { if (window.confirm('删?')) go(); }}/></div>;\n"
     "}"),
    ("🔴 JSX 自闭合 /> 之后的调用点",
     "export function A(){\n"
     "  return <div><Icon className=\"h-4\" /><button onClick={() => { if (confirm('删?')) go(); }}/></div>;\n"
     "}"),
    ("除法运算之后的调用点",
     "export function A(a: number, b: number){ const r = (a+b)/2; if (!confirm('删?')) return r; }"),
]

# ---------------------------------------------------------------- 必须不命中
MUST_NOT_HIT = [
    ("行注释里提到 confirm(",
     "// 取消 confirm() 浏览器弹窗\nexport const A = 1;"),
    ("块注释里提到 alert(",
     "/* 旧写法 alert('x') 已废弃\n   见工单 */\nexport const A = 1;"),
    ("字符串字面量里含 alert( —— fixture 的真实形态",
     "export const S = '- 危险的 javascript 链接 javascript:alert(1) 应被禁用';"),
    ("应用内 useConfirmDialog 的 await confirm({...})",
     "import { useConfirmDialog } from '@/components/ui/confirm-dialog';\n"
     "export function A(){\n"
     "  const [confirmDialog, confirm] = useConfirmDialog();\n"
     "  const go = async () => { const ok = await confirm({ title: '删?' }); return ok; };\n"
     "  return <div>{confirmDialog}</div>;\n"
     "}"),
    ("局部同名业务函数 const confirm = async () => {}(ProviderDowngradeWizard 的真实形态)",
     "export function A(){\n"
     "  const confirm = async () => { await post(); };\n"
     "  return <button onClick={() => void confirm()} />;\n"
     "}"),
    ("模板串正文里出现 confirm(",
     "export const S = `旧文案:confirm('删?') 已经不用了`;"),
]


def test_scanner_must_hit(tmp_path):
    m = _scanner()
    failures = []
    for i, (name, src) in enumerate(MUST_HIT):
        d = tmp_path / f"hit{i}" / "src"
        d.mkdir(parents=True, exist_ok=True)
        (d / "Probe.tsx").write_text(src, encoding="utf-8")
        if not m.scan(d):
            failures.append(name)
    assert not failures, (
        "扫描器**漏报**下列形态 —— 漏报意味着「扫过了」是假绿:\n  " + "\n  ".join(failures)
    )


def test_scanner_must_not_hit(tmp_path):
    m = _scanner()
    failures = []
    for i, (name, src) in enumerate(MUST_NOT_HIT):
        d = tmp_path / f"miss{i}" / "src"
        d.mkdir(parents=True, exist_ok=True)
        (d / "Probe.tsx").write_text(src, encoding="utf-8")
        hits = m.scan(d)
        if hits:
            failures.append(f"{name} → 误报 {[h.kind for h in hits]}")
    assert not failures, (
        "扫描器**误报**下列形态 —— 恒红和恒绿一样废,一个让人无视,一个让人误信:\n  "
        + "\n  ".join(failures)
    )


def test_stripper_does_not_swallow_following_lines(tmp_path):
    """🔴 直接锁住那个真正的病根:剥离器**不许把后续代码整段吞掉**。

    上面两组样本是黑盒判定;这条是白盒的 —— 剥完之后,后面那行代码必须还在。
    两次事故都是「状态机失步 → 后面几十行被清空」,黑盒样本可能恰好躲开。
    """
    m = _scanner()
    src = (
        "const re = /[#*`>\\[\\]()|_~-]/g;\n"
        "const jsx = <div><Card>x</Card><Icon className=\"h-4\" /></div>;\n"
        "const MARKER_MUST_SURVIVE = 1;\n"
    )
    stripped = m.strip_noncode(src)
    assert "MARKER_MUST_SURVIVE" in stripped, (
        "剥离器把正则/JSX 之后的代码吞掉了 —— 这正是 WritingHall / OrganizationCenter 漏报的病根"
    )
    assert len(stripped) == len(src), "剥离器必须保长度,否则行号回算全错"
    assert stripped.count("\n") == src.count("\n"), "剥离器必须保换行"
