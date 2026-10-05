#!/usr/bin/env bash
# =============================================================================
# gate_served_assets_scan.sh —— 发车时「对外服务的产物」真实客户数据扫描(WO_264 §3-②)
#
# ## 为什么要这道门
# 2026-09-22:真实客户诊断报告与公司名进了公开前端包。WO_256 当天修好了**源码**,
# **暴露却又持续了一整天** —— 因为产物留在一个从不清理的存档里,而容器 nginx:
#
#     location /assets/     { root /app/frontend/dist;  try_files $uri @assets_archive; }
#     location @assets_archive { root /app/assets_archive; try_files $uri =404; }
#
# 回落**不看代次**:`merge_frontend_assets.py` 的 KEEP_GENERATIONS 只约束「合并进 dist」,
# 存档里有什么就对外服务什么。部署日志那句 `skipped_expired_files: 227`
# 只说明「这一代没合进 dist」,**不代表取不到** —— 它们照样在 @assets_archive 那层匿名 200。
#
# 🔴 所以本门的分母是 **存档 ∪ 活跃槽 dist 的并集**,不是镜像 dist 的那 272 个。
#    只列 dist 会得到假绿(WO_264 §4-4:开单人 2026-09-22 就这么得过一次)。
#
# ## 跑在哪
# **生产机**,发车流程 `deploy-blue-green.sh` 的 `[3.5/6]` 合并之后、`[4/6]` 切槽之前。
# 那台机器 python 是 3.6(`subprocess.run(capture_output=)` 3.7 才有)、没有仓内 node
# ⇒ 本门只用 bash + grep,不依赖 node / 新 python。
#
# ## 🔴 locale:本门最容易假绿/假红的地方
# 形状规则是**字符类** `[一-龥]{2,12}(有限公司|…)`。它在 UTF-8 locale 下按码位匹配,
# 在 `LC_ALL=C` 下**按字节切**。实测(同一份合成名):
#     C.UTF-8 : 云舟出行服务有限公司 → 全名
#     LC_ALL=C: 云舟出行服务有限公司 → **出行服务有限公司**(前两字被切)
#               而且 **rc=0、条数还对、不报错**
# 截断后的名字对不上白名单 ⇒ 判成「白名单外命中」⇒ **假红拦车**。
# ⇒ 本门①自设 UTF-8 locale;②**必做逐字节自校准**,校不过 rc=3 不给判定。
#    不强设 locale 会每班 rc=3(生产机就是 C),等于门不存在;
#    设了再校准,「这台机器连 C.UTF-8 都没有」照样会被喊出来,不掩盖。
#
# ## 退出码(三态)
#   0 = 白名单外命中 0
#   1 = 有白名单外命中 ⇒ **不许切槽**
#   3 = 没跑成(存档/dist 取不到、白名单读不到、自校准不过)—— 没跑 != 没违规
#
# ## 用法
#   gate_served_assets_scan.sh --repo <仓根> [--archive <目录>] [--dist <目录>]
# =============================================================================
set -u

# ---- ① locale:先设,再校准
if locale -a 2>/dev/null | grep -qix 'C.UTF-8\|C.utf8'; then
    export LC_ALL=C.UTF-8
elif locale -a 2>/dev/null | grep -qix 'en_US.UTF-8\|en_US.utf8'; then
    export LC_ALL=en_US.UTF-8
fi
_LOCALE_USED="${LC_ALL:-<未设:沿用环境>}"

REPO=""; ARCHIVE="/opt/omnirank/geo_agentscope/assets_archive"; DIST=""; CONTAINER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --repo)      REPO="$2"; shift 2 ;;
        --archive)   ARCHIVE="$2"; shift 2 ;;
        --dist)      DIST="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;   # 从容器里取 dist(见下)
        *) echo "  🔴 未知参数:$1"; exit 3 ;;
    esac
done

die3() { echo "  🔴 本门【没跑成·rc=3】:$* —— 没跑 != 没违规"; exit 3; }

[ -n "$REPO" ] || die3 "必须传 --repo <仓根>"
ALLOW_FILE="$REPO/frontend/scripts/synthetic-allow.txt"
[ -f "$ALLOW_FILE" ] || die3 "读不到白名单 $ALLOW_FILE(它是 WO_264 ② 抽出的单一来源)"

WORK=$(mktemp -d) || die3 "建不了临时目录"
trap 'rm -rf "$WORK"' EXIT

# ---- 形状模式文件。🔴 三控与真判据**共用同一条代码路径**:
#      都走 `scan_with <模式文件> <目录…>`,不把 -e 拼进变量。
#      拼 -e 会让「控件走的那条路」和「真判据走的那条路」不是同一条 ——
#      2026-09-22 本仓同日两次踩过「写死的控件双双通过、而真判据那条路径是坏的」。
SHAPE_PAT="$WORK/shape.pat"
printf '%s\n' '[一-龥]{2,12}(有限公司|股份有限公司|集团)' > "$SHAPE_PAT"

# 白名单(去注释去空行)
ALLOW_LIST="$WORK/allow.list"
grep -v '^[[:space:]]*#' "$ALLOW_FILE" | sed 's/[[:space:]]*$//' | grep -v '^$' > "$ALLOW_LIST"
ALLOW_N=$(wc -l < "$ALLOW_LIST" | tr -d ' ')
[ "$ALLOW_N" -ge 4 ] || die3 "白名单只有 ${ALLOW_N} 条(应 ≥4)—— 清单不对,读数不可信"

# ---- 唯一的扫描实现。三控与真判据都调它。
#      输出:每行 `<文件> <命中数>`;只输出**有命中**的文件。
scan_with() {
    _pat="$1"; shift
    for _d in "$@"; do
        [ -d "$_d" ] || continue
        find "$_d" -type f -name '*.js' -print0 2>/dev/null |
        while IFS= read -r -d '' _f; do
            _n=$(grep -o -E -f "$_pat" "$_f" 2>/dev/null | wc -l | tr -d ' ')
            [ "${_n:-0}" -gt 0 ] && printf '%s\t%s\n' "$_f" "$_n"
        done
    done
}
# 抽出命中的**去重值**(用于比对白名单)
uniq_hits() {
    _pat="$1"; shift
    for _d in "$@"; do
        [ -d "$_d" ] || continue
        find "$_d" -type f -name '*.js' -print0 2>/dev/null |
        xargs -0 -r grep -h -o -E -f "$_pat" 2>/dev/null
    done | sort -u
}

# ---- ② 自校准:逐字节。校不过 ⇒ rc=3,不给判定。
CAL_DIR="$WORK/cal"; mkdir -p "$CAL_DIR"
CAL_NAME=$(head -1 "$ALLOW_LIST")
printf 'var a="%s";\n' "$CAL_NAME" > "$CAL_DIR/calib.js"
CAL_OUT=$(uniq_hits "$SHAPE_PAT" "$CAL_DIR")
if [ "$CAL_OUT" != "$CAL_NAME" ]; then
    echo "  🔴 locale 自校准失败(生效 locale=${_LOCALE_USED})"
    echo "     喂入:$(printf '%s' "$CAL_NAME" | wc -c) 字节 / 取回:$(printf '%s' "$CAL_OUT" | wc -c) 字节"
    echo "     ⇒ 字符类按字节切了,名字会被截断 ⇒ 截断后对不上白名单 ⇒ **假红拦车**"
    die3 "字符类匹配在本机 locale 下不按码位走"
fi
echo "  ✅ locale 自校准:生效 locale=${_LOCALE_USED} · 样名逐字节相等($(printf '%s' "$CAL_NAME" | wc -c) 字节)"

# ---- 分母:存档 ∪ 活跃槽 dist
#
# 🔴 dist **多半烤在镜像里、宿主看不见**。第一版打算用
#    `docker inspect -f '{{range .Mounts}}…'` 取挂载点 —— 那是猜的:
#    不是 bind mount 时它返回空,而门会**照常绿**,分母悄悄退化成「只有存档」。
#    「少了一半分母」与「两边都干净」在读数上长得一样。
#    ⇒ 传了 --container 就**必须**取得到:取不到 ⇒ rc=3,不给判定。
if [ -n "$CONTAINER" ]; then
    DIST="$WORK/slotdist"
    mkdir -p "$DIST"
    if ! docker cp "$CONTAINER:/app/frontend/dist/assets/." "$DIST/" >/dev/null 2>&1; then
        die3 "传了 --container $CONTAINER 但取不出 /app/frontend/dist/assets —— 分母会只剩存档"
    fi
    _dn=$(find "$DIST" -type f -name '*.js' 2>/dev/null | wc -l | tr -d ' ')
    [ "${_dn:-0}" -ge 1 ] || die3 "从 $CONTAINER 取到的 dist 里一个 .js 都没有 —— 分母不对"
fi

# 🔴🔴 存档那一侧**必须取得到**,取不到 ⇒ rc=3,**不是绿**。
#   第一版只在「存档与 dist 都取不到」时才 rc=3 ⇒ `--archive <路径写错了>`
#   会让门只扫 dist、打印「全扫过、0 命中」、**rc=0 绿**。
#   而发车时不传 --archive 走默认路径:路径一改、目录没挂上,分母就悄悄只剩 dist ——
#   **那正是本门存在的理由**(WO_256 修好源码后暴露又活一整天,就活在存档这一侧)。
#   「少了一半分母」与「两边都干净」在读数上长得一样。
#
#   ⇒ 我在交付单里写过一句「存档读到 0 是路径不对,不是干净」——
#     那句话当时是写给**人**看的提醒。本门抬头就写着「注释传不出去,
#     只有会自己报错的东西才拦得住」,所以它现在是**门的规则**:
[ -d "$ARCHIVE" ] || die3 "存档目录取不到:$ARCHIVE(发车默认路径;路径不对或没挂上)"
ARCH_N=$(find "$ARCHIVE" -type f -name '*.js' 2>/dev/null | wc -l | tr -d ' ')
[ "${ARCH_N:-0}" -ge 1 ] || die3 "存档 $ARCHIVE 里一个 .js 都没有 —— 按 WO_264 §1 实测应在 5,700 上下,读到 0 是路径不对不是干净"

SCOPE="$ARCHIVE"
[ -n "$DIST" ] && [ -d "$DIST" ] && SCOPE="$SCOPE $DIST"
N_FILES=0
for _d in $SCOPE; do
    _c=$(find "$_d" -type f -name '*.js' 2>/dev/null | wc -l | tr -d ' ')
    _tag=""; [ "$_d" = "$ARCHIVE" ] && _tag="(存档)" || _tag="(本槽 dist)"
    echo "  分母${_tag}:$_d → ${_c} 个 .js"
    N_FILES=$((N_FILES + _c))
done
[ "$N_FILES" -ge 1 ] || die3 "分母里一个 .js 都没有 —— 读数不可信"

# ---- 三控(与真判据同一条 scan_with 路径)
CTRL_DIR="$WORK/ctrl"; mkdir -p "$CTRL_DIR"
printf 'function f(){return 1}\n' > "$CTRL_DIR/pos.js"
printf 'var x="%s";\n' "$CAL_NAME" > "$CTRL_DIR/cal.js"
printf 'var y="zz_not_a_company_zzz";\n' > "$CTRL_DIR/neg.js"

POS_PAT="$WORK/pos.pat"; printf 'function\n' > "$POS_PAT"
_pos=$(scan_with "$POS_PAT" "$CTRL_DIR" | wc -l | tr -d ' ')
_cal=$(scan_with "$SHAPE_PAT" "$CTRL_DIR" | wc -l | tr -d ' ')
_neg=$(uniq_hits "$SHAPE_PAT" "$CTRL_DIR" | grep -c 'zz_not_a_company_zzz' || true)
echo "  三控(同一 scan_with 路径):正控 function 命中 ${_pos} 文件 · 校准合成名命中 ${_cal} 文件 · 负控编造串 ${_neg}(应 0)"
[ "${_pos:-0}" -ge 1 ] || die3 "正控不响(function 都扫不到)⇒ 扫描路径是坏的,结论作废"
[ "${_cal:-0}" -ge 1 ] || die3 "校准控不响(白名单合成名都扫不到)⇒ 形状规则没生效,结论作废"
[ "${_neg:-0}" = 0 ]   || die3 "负控误报(编造串被当成公司名)⇒ 规则太宽,结论作废"

# ---- 真判定
HITS="$WORK/hits.tsv"
scan_with "$SHAPE_PAT" $SCOPE > "$HITS" 2>/dev/null
BAD_VALS=$(uniq_hits "$SHAPE_PAT" $SCOPE | grep -v -x -F -f "$ALLOW_LIST" || true)
BAD_N=$(printf '%s\n' "$BAD_VALS" | grep -c . || true)

TOTAL_HIT_FILES=$(wc -l < "$HITS" | tr -d ' ')
echo "  形状命中:${TOTAL_HIT_FILES} 个文件(分母 ${N_FILES} 个 .js)· 白名单 ${ALLOW_N} 条"

if [ "${BAD_N:-0}" -gt 0 ]; then
    echo "  🔴 **白名单外**的工商主体形状命中 ${BAD_N} 个(不抄值,只报个数与文件):"
    # 逐文件列出「含白名单外命中」的那些
    while IFS=$'\t' read -r _f _n; do
        _b=$(grep -o -E -f "$SHAPE_PAT" "$_f" 2>/dev/null | sort -u \
             | grep -v -x -F -f "$ALLOW_LIST" | grep -c . || true)
        [ "${_b:-0}" -gt 0 ] && echo "       $(basename "$_f")  形状命中 ${_n} 处 / 白名单外 ${_b} 个"
    done < "$HITS"
    echo "  🔴 **不许切槽**:这些产物正在被 @assets_archive 或活跃槽 dist 对外服务。"
    echo "     处置:先把命中文件从存档/dist 移除并复扫为 0,再继续发车;"
    echo "     边缘层(阿里云 ESA)另需 PurgeCaches,验收看内容不看状态码。"
    exit 1
fi

echo "  ✅ 分母 ${N_FILES} 个 .js 全扫过,白名单外命中 0"
exit 0
