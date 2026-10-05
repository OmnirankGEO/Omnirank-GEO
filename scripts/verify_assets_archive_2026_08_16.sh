#!/bin/bash
# [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16 ③] 部署后判据:上一版 chunk 仍可取。
#
# 🔴 本脚本**必须在生产上跑**,本地跑不了:它验的是 nginx 的 @assets_archive 回落
#    + start.sh 的归档动作,两者都只在容器里存在。
#
# ── 🔴 [返修 2026-08-18] 上一版在**健康的生产上报 FAIL** ─────────────────────
# 实测(生产尖 ecce9985 · blue):
#     当前版 chunk: /assets/index-BDtwwQs_.js
#     归档里的上一版: index-1lJUV6Eq.js
#     正向  上一版 chunk → 200 · X-Omnirank-Assets=<none>   ← 判 FAIL
# 而 `index-1lJUV6Eq.js` **就在当前 dist 里**(实测 `ls` 命中)⇒ 它压根不是"上一版"。
#
# 死因:上一版用 `ls /app/assets_archive/assets | grep "^index-" | grep -v <当前入口>`
# 挑"上一版",默认「叫 index-* 的只有入口 chunk 一个」。**这个默认是错的** ——
# vite 按源码目录给懒加载 chunk 命名,`pages/<X>/index.tsx` 一律产出 `index-<hash>.js`,
# 实测当前 dist 里叫 `index-*.js` 的有 **53 个**。挑中的几乎必然是**当前版**的某个懒加载块
# ⇒ 走 dist 命中 ⇒ 没有 archive 头 ⇒ 恒报「正向失败」。
#
# 🔴 教训不是"选错了一个文件"。是**判据没有分母**:
#   「回落有没有生效」的分母 = **归档有、而 dist 没有**的那批文件(只有它们会触发回落)。
#   上一版从没算过这个集合,而是**猜**了一个代表元。猜错时它报红,
#   而"在健康系统上报红"的判据比没有判据更坏 —— 下一个人要么白烧一轮,要么学会无视它。
#
# 修法:分母改成机械求集合差 `archive - dist`,空集就 exit 3 **拒判**(不是通过、也不是失败),
#      并顺带打印拒判的**原因诊断**,不让下一个人再推一遍。
#
# 判据成对(每条"必须命中"都配了"必须不命中"):
#   正向   archive-dist 差集里的真文件 → 200 + X-Omnirank-Assets: archive
#          且**正文与归档盘上那份逐字节同哈希**(200 只说明有人回了,哈希才证明 root 拼对了路径)
#   反向A  不存在的 hash → 404(回落不是"什么都返 200" —— 那会把 MIME 错误引回来,
#          正是 2026-05-20 那次 P0 救火的形态)
#   反向B  当前版入口 chunk → 200 且**不带** archive 头(新资产仍走 dist,没被归档遮住)
#   反向C  dist 与 archive **都有**的文件 → 200 且**不带** archive 头(dist 优先,回落不抢)
#
# 用法:runner -RemoteFile verify_assets_archive_2026_08_16.sh          # 自动选槽
#      runner -RemoteFile verify_assets_archive_2026_08_16.sh green    # 指定槽
set +e

# ── 判据本体:一把尺子,主流程与 --selftest 共用 ─────────────────────────────
# 🔴 刻意做成函数而不是拷两份:自检若用**另一份**实现,它证明的就不是这把尺子。
# 返回码与整脚本一致:0 通过 · 1 失败 · 3 无分母拒判。
assert_target() {
    local C="$1" BASE="$2" DIAG="${3:-}" FAIL=

    docker exec "$C" sh -c 'ls -1 /app/frontend/dist/assets 2>/dev/null | sort > /tmp/_vaa_dist.txt
                            ls -1 /app/assets_archive/assets 2>/dev/null | sort > /tmp/_vaa_arch.txt
                            comm -13 /tmp/_vaa_dist.txt /tmp/_vaa_arch.txt > /tmp/_vaa_onlyarch.txt' 2>/dev/null
    local DIST_N ARCH_N ONLY_N
    DIST_N=$(docker exec "$C" sh -c 'wc -l < /tmp/_vaa_dist.txt' 2>/dev/null | tr -d ' \r')
    ARCH_N=$(docker exec "$C" sh -c 'wc -l < /tmp/_vaa_arch.txt' 2>/dev/null | tr -d ' \r')
    ONLY_N=$(docker exec "$C" sh -c 'wc -l < /tmp/_vaa_onlyarch.txt' 2>/dev/null | tr -d ' \r')
    echo "分母 · dist=${DIST_N:-0} · archive=${ARCH_N:-0} · 只在归档(= 会触发回落的)=${ONLY_N:-0}"

    if [ "${DIST_N:-0}" -eq 0 ]; then
        echo "🔴 dist 为空 —— 后面每一条都无意义,不按通过计"; return 3
    fi

    if [ "${ONLY_N:-0}" -eq 0 ]; then
        echo "🔴 归档里没有任何『dist 没有』的文件 ⇒ 回落永远不会被触发 ⇒ **本判据没有分母**。"
        echo "   这既不是通过也不是失败。可能原因,按顺序自查:"
        echo "   ① 首次部署(归档里只有当前版)—— 第二次部署后再跑;"
        local WR
        WR=$(docker diff "$C" 2>/dev/null | grep -c '/app/frontend/dist/assets')
        echo "   ② 该槽 dist 被运行时写过:可写层里 dist/assets 条目数 = ${WR}"
        if [ "${WR:-0}" -gt 0 ]; then
            echo "      🔴 命中 ② —— 有人往**运行中容器**的 dist 里塞过旧资产(docker cp 之类)。"
            echo "      后果:回落被遮住(dist 先命中)⇒ 本判据在该槽上永远没有分母;"
            echo "      且那些文件只活在**容器可写层**里,容器一重建就没,归档并不含它们。"
            [ -n "$DIAG" ] && echo "      ⇒ 改打另一个槽:$DIAG"
        fi
        echo "   ③ 归档段没跑(看 docker logs ${C} | grep assets-archive)"
        docker logs "$C" 2>&1 | grep -i 'assets-archive' | tail -2
        return 3
    fi

    # ── 1. 正向:差集里的真文件必须走回落,且正文逐字节正确 ──────────────────
    echo
    echo "-- 正向:归档独有的资产 → 200 + archive 头 + 正文同哈希 --"
    local CAND f code hdr h_http h_disk same
    CAND=$(docker exec "$C" sh -c 'grep "\.js$" /tmp/_vaa_onlyarch.txt | head -3' 2>/dev/null | tr -d '\r')
    if [ -z "$CAND" ]; then
        echo "🔴 差集里没有 .js(只有图片/css?)—— 判据形态不成立,不按通过计"; return 3
    fi
    for f in $CAND; do
        code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/assets/${f}")
        hdr=$(curl -sI "${BASE}/assets/${f}" | tr -d '\r' | grep -i '^X-Omnirank-Assets:' | awk '{print $2}')
        h_http=$(curl -s "${BASE}/assets/${f}" | sha256sum | awk '{print $1}')
        h_disk=$(docker exec "$C" sh -c "sha256sum /app/assets_archive/assets/${f}" 2>/dev/null | awk '{print $1}')
        same=$([ -n "$h_disk" ] && [ "$h_http" = "$h_disk" ] && echo yes || echo no)
        echo "   ${f} → ${code} · X-Omnirank-Assets=${hdr:-<none>} · 正文同哈希=${same}"
        [ "$code" = "200" ] && [ "$hdr" = "archive" ] && [ "$same" = "yes" ]             || { echo "   🔴 正向失败(${f})"; FAIL=1; }
    done

    # ── 2. 反向A:回落不许变成万能 200 ──────────────────────────────────────
    echo
    echo "-- 反向A:不存在的 hash 必须 404 --"
    local code404
    code404=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/assets/index-DOESNOTEXIST0000.js")
    echo "   不存在的 hash → ${code404}(须 404)"
    [ "$code404" = "404" ] || { echo "   🔴 回落变成了『什么都返 200』—— MIME 死循环会回来"; FAIL=1; }

    # ── 3. 反向B:当前版资产不许被归档遮住 ──────────────────────────────────
    echo
    echo "-- 反向B:当前版入口 chunk 必须 200 且【无】archive 头 --"
    local CUR codecur hdrcur
    CUR=$(curl -s "${BASE}/" | grep -o '/assets/index-[A-Za-z0-9_-]*\.js' | head -1)
    if [ -z "$CUR" ]; then
        echo "   🔴 取不到当前入口 chunk —— 这一条无法成立"; FAIL=1
    else
        codecur=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}${CUR}")
        hdrcur=$(curl -sI "${BASE}${CUR}" | tr -d '\r' | grep -i '^X-Omnirank-Assets:' | awk '{print $2}')
        echo "   ${CUR} → ${codecur} · X-Omnirank-Assets=${hdrcur:-<none>}"
        [ "$codecur" = "200" ] && [ -z "$hdrcur" ] || { echo "   🔴 新资产被归档遮住了"; FAIL=1; }
    fi

    # ── 4. 反向C:两边都有时 dist 必须优先 ──────────────────────────────────
    # 🔴 这条是正向的**成对反向**:证明 archive 头不是"凡是归档里有的都盖章",
    #    而是真的只在 dist 未命中时才出现。少了它,正向那条可能是恒真。
    echo
    echo "-- 反向C:dist 与 archive 都有的文件 → 200 且【无】archive 头(dist 优先)--"
    local BOTH codeb hdrb
    BOTH=$(docker exec "$C" sh -c 'comm -12 /tmp/_vaa_dist.txt /tmp/_vaa_arch.txt | grep "\.js$" | head -1' 2>/dev/null | tr -d '\r')
    if [ -z "$BOTH" ]; then
        echo "   ⚠️ 两边都有的 .js 为空 —— 这一条无分母(不计失败,但也别当它验过了)"
    else
        codeb=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/assets/${BOTH}")
        hdrb=$(curl -sI "${BASE}/assets/${BOTH}" | tr -d '\r' | grep -i '^X-Omnirank-Assets:' | awk '{print $2}')
        echo "   ${BOTH} → ${codeb} · X-Omnirank-Assets=${hdrb:-<none>}"
        [ "$codeb" = "200" ] && [ -z "$hdrb" ] || { echo "   🔴 dist 没有优先,回落抢了当前资产"; FAIL=1; }
    fi

    [ -n "$FAIL" ] && return 1
    return 0
}
# ── selftest:证明这把尺子有判别力(成对 —— 好的必须绿,坏的必须红)────────
# 🔴 存在的理由:上一版在**健康的生产上报 FAIL**,而三方都没发现,
#   因为没人问过「这把尺子在系统真坏的时候会不会红、在真好的时候会不会绿」。
#   自检用**临时容器**跑,全程不碰 blue/green 两槽,退出时无条件清理。
#   临时 nginx 只跑静态段(--entrypoint 直接起 nginx),不连 DB/Redis,不占公网端口。
if [ "$1" = "--selftest" ]; then
    IMG=$(docker inspect -f '{{.Image}}' omnirank-blue 2>/dev/null)
    ARCH_SRC=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/assets_archive"}}{{.Source}}{{end}}{{end}}' omnirank-blue 2>/dev/null)
    [ -z "$IMG" ] || [ -z "$ARCH_SRC" ] && { echo "🔴 selftest 取不到镜像/归档挂载源,拒跑"; exit 3; }
    echo "== selftest · 镜像=${IMG:7:12} · 归档源=${ARCH_SRC} =="

    PORT_OK=18099; PORT_BAD=18098
    # 🔴 容器名不许以 `_` 开头(docker: Invalid container name)—— 第一版用 `_vaa_ok` 当场 rc=125
    # 🔴 先清上一轮残留容器,**再**建临时目录 —— 反过来写会把刚建的目录自己删掉
    #   (第一版就这么写的,实测 `ok.conf: No such file or directory` 当场拒跑)。
    docker rm -f vaa-selftest-ok vaa-selftest-bad >/dev/null 2>&1
    TMPD=$(mktemp -d)
    cleanup() {
        docker rm -f vaa-selftest-ok vaa-selftest-bad >/dev/null 2>&1
        rm -rf "$TMPD"
    }
    trap cleanup EXIT INT TERM

    # 取镜像里那份真 conf 当基准(不是仓库里的 —— 要证明的是**镜像里跑的**那份)
    docker run --rm --entrypoint sh "$IMG" -c 'cat /etc/nginx/conf.d/default.conf' > "$TMPD/ok.conf" 2>/dev/null
    [ -s "$TMPD/ok.conf" ] || { echo "🔴 取不到镜像内 conf,拒跑"; exit 3; }

    # 变异:把 /assets/ 的回落摘掉(回到本包之前的行为)
    sed 's#try_files \$uri @assets_archive;#try_files $uri =404;#' "$TMPD/ok.conf" > "$TMPD/bad.conf"
    if cmp -s "$TMPD/ok.conf" "$TMPD/bad.conf"; then
        echo "🔴 变异没落地(两份 conf 逐字相同)—— 这一轮的红和绿都不算数,拒判"; exit 3
    fi
    echo "变异已落地:conf 差异行数 = $(diff "$TMPD/ok.conf" "$TMPD/bad.conf" | grep -c '^[<>]')"

    start_tmp() {   # $1=名字 $2=conf $3=端口
        docker run -d --name "$1" -p "127.0.0.1:$3:80" \
            -v "$ARCH_SRC:/app/assets_archive:ro" \
            -v "$2:/etc/nginx/conf.d/default.conf:ro" \
            --entrypoint sh "$IMG" -c 'nginx -g "daemon off;"' >/dev/null 2>&1
        for _ in $(seq 1 20); do
            [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$3/")" = "200" ] && return 0
            sleep 0.5
        done
        return 1
    }

    echo
    echo "---- ①正样本:镜像原 conf(回落在)· 期望 rc=0 ----"
    if ! start_tmp vaa-selftest-ok "$TMPD/ok.conf" $PORT_OK; then
        echo "🔴 正样本容器起不来 —— harness 自己坏了,后面的『红』不能当证据"; exit 3
    fi
    assert_target vaa-selftest-ok "http://127.0.0.1:$PORT_OK"; RC_OK=$?
    echo "   → rc=$RC_OK"

    echo
    echo "---- ②负样本:摘掉回落的 conf · 期望 rc=1 ----"
    if ! start_tmp vaa-selftest-bad "$TMPD/bad.conf" $PORT_BAD; then
        echo "🔴 负样本容器起不来 —— 它的红可能只是没起来,不算判别力"; exit 3
    fi
    assert_target vaa-selftest-bad "http://127.0.0.1:$PORT_BAD"; RC_BAD=$?
    echo "   → rc=$RC_BAD"

    echo
    echo "== selftest 结论:正样本 rc=${RC_OK}(须 0)· 负样本 rc=${RC_BAD}(须 1)=="
    if [ "$RC_OK" = "0" ] && [ "$RC_BAD" = "1" ]; then
        echo "✅ 这把尺子两个方向都会说话 —— 好系统判绿、坏系统判红"; exit 0
    fi
    echo "🔴 判别力不成立:$([ "$RC_OK" != "0" ] && echo '好系统被判成非绿(会重演"健康生产报 FAIL")')$([ "$RC_BAD" != "1" ] && echo ' 坏系统没被判红(恒绿)')"
    exit 1
fi

# ── 选槽 ──────────────────────────────────────────────────────────────────
# 不传参时默认打**活跃槽**(用户真正在访问的那个)。活跃槽由 nginx 上游实测,不看记忆。
SLOT="$1"
if [ -z "$SLOT" ]; then
    UP=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/ 2>/dev/null)
    SLOT=$([ "$UP" = "200" ] && echo blue || echo green)
    echo "(未指定槽 · 自动选 ${SLOT})"
fi
PORT="$([ "$SLOT" = "green" ] && echo 8002 || echo 8001)"
OTHER="$([ "$SLOT" = "blue" ] && echo green || echo blue)"

echo "== 归档回落判据 · slot=${SLOT} (http://127.0.0.1:${PORT}) =="
assert_target "omnirank-${SLOT}" "http://127.0.0.1:${PORT}" "$0 ${OTHER}"
RC=$?

echo
case "$RC" in
    0) echo "== 结论:✅ 归档回落生效(正向同哈希)· 未变成万能 200 · 未遮住当前资产 ==" ;;
    3) echo "== 结论:⏸ 无分母 · 拒判(既不是通过也不是失败)==" ;;
    *) echo "== 结论:🔴 FAIL ==" ;;
esac
exit $RC
