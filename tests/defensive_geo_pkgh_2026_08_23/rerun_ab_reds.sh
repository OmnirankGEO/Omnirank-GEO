#!/usr/bin/env bash
# 【包H R2 · §2.4.1 补验逐字复跑】三条 A/B 红文件,每文件一把**全新**库。
# 这不是"示意命令" —— 交付单里贴的就是这一份,逐字可粘贴。
set -u
cd "$(dirname "$0")/../.."      # 仓库根
OUT=/c/AI-Test/_ab_pkgh_20260824
IMG=pgvector/pgvector:pg16

run_one () {                       # $1=文件名  $2=端口  $3=容器名
  local file="$1" port="$2" name="$3"
  docker rm -f "$name" >/dev/null 2>&1
  docker run -d --name "$name" \
      -e POSTGRES_USER=geo_admin -e POSTGRES_PASSWORD=testpw \
      -e POSTGRES_DB=geo_defgeo_test -p "$port":5432 "$IMG" >/dev/null
  # 🔴 不 sleep 猜:轮询 pg_isready,起不来就直接失败(别让"没跑"混成"绿")
  for _ in $(seq 1 60); do
      docker exec "$name" pg_isready -U geo_admin -d geo_defgeo_test >/dev/null 2>&1 && break
      sleep 1
  done
  docker exec "$name" pg_isready -U geo_admin -d geo_defgeo_test >/dev/null 2>&1 || {
      echo "🔴 $name 起不来 —— 这一条**没跑**"; return 1; }

  TEST_DATABASE_URL="postgresql://geo_admin:testpw@localhost:${port}/geo_defgeo_test" \
    python -m pytest "tests/defensive_geo_w3_2026_08_21/${file}" \
      -q -p no:cacheprovider --junitxml="${OUT}/rerun_${file%.py}.xml"
  local rc=$?
  docker rm -f "$name" >/dev/null 2>&1        # 一次性库,跑完就删(别攒成陈旧队列)
  # 🔴 junit 分母 > 0 —— 本仓两次假绿都是"一个 xml 都没写出来"
  python -c "import sys,xml.etree.ElementTree as E; r=E.parse(sys.argv[1]).getroot(); s=r[0] if r.tag=='testsuites' else r; t=int(s.get('tests','0')); print(f'   junit: tests={t} failures={s.get(\"failures\")} errors={s.get(\"errors\")} skipped={s.get(\"skipped\")}'); sys.exit(0 if t>0 else 1)" "${OUT}/rerun_${file%.py}.xml" || { echo "🔴 junit 分母为 0 —— 这一条**没跑**"; return 1; }
  return $rc
}

bad=0
run_one test_wp5_copy_and_actions.py 55497 pkgh-r2-55497 || bad=1
run_one test_wp5_publish_http_pg.py  55498 pkgh-r2-55498 || bad=1
run_one test_wp6_settlement_pure.py  55499 pkgh-r2-55499 || bad=1
echo "================================"
[ $bad -eq 0 ] && echo "✅ 三条补验全绿" || echo "🔴 有条目未通过/没跑"
exit $bad
