from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_agent_routes_require_agent_guard():
    src = read("frontend/src/App.tsx")
    for route in [
        'path="agent/inventory"',
        'path="agent/pricing"',
        'path="agent/settlement"',
        'path="agent/promotion"',
    ]:
        idx = src.index(route)
        snippet = src[idx:idx + 500]
        assert "requiresAgent" in snippet


def test_feedback_inbox_shows_error_instead_of_empty_on_failure():
    src = read("frontend/src/pages/Admin/AiOpsCenter/components/FeedbackInbox.tsx")
    assert "反馈加载失败" in src
    assert "setError" in src
    assert "status: 'pending'" in src
    assert "没有未处理的 bug 反馈" in src


def test_ai_ops_mobile_uses_select_and_hides_desktop_nav():
    nav = read("frontend/src/pages/Admin/AiOpsCenter/components/AiOpsNav.tsx")
    page = read("frontend/src/pages/Admin/AiOpsCenter/index.tsx")
    assert "hidden shrink-0" in nav
    assert "lg:flex" in nav
    assert "NAV_ITEMS" in page
    assert 'id="aiops-section"' in page
    assert "lg:hidden" in page


def test_public_quote_computes_missing_subtotal_from_quantity_and_unit_price():
    src = read("frontend/src/pages/Public/PublicQuote.tsx")
    assert "function getServiceSubtotal" in src
    assert "getServiceQuantity(svc) * getServiceUnitPrice(svc)" in src
    assert "explicit !== null && (explicit > 0 || calculated <= 0)" in src
    assert "后端 total_price 仍是总价 SSOT" in src


def test_pricing_context_is_scoped_to_real_consumer_routes():
    src = read("frontend/src/context/PricingContext.tsx")
    assert "activate: () => () => void" in src
    assert "const [consumerCount, setConsumerCount] = useState(0)" in src
    assert "const hasConsumers = consumerCount > 0" in src
    assert "authLoading || pricingScope === null || !hasConsumers" in src
    assert "useEffect(() => ctx?.activate(), [ctx?.activate])" in src
    assert "const { user, isLoading: authLoading, authorizationScope } = useAuth()" in src
    assert "const pricingScope = userId === null ? null : authorizationScope" in src
    assert "localStorage.getItem('portal_token')" not in src
    assert "localStorage.getItem('omnirank_token')" not in src


def test_sensitive_frontend_caches_follow_authoritative_auth_generation():
    auth = read("frontend/src/context/AuthContext.tsx")
    clients = read("frontend/src/context/ClientContext.tsx")
    wallet = read("frontend/src/context/WalletContext.tsx")
    brands = read("frontend/src/hooks/useBrandsTestMap.ts")
    home = read("frontend/src/hooks/useHomeStats.ts")
    assert "authorizationScope: string" in auth
    assert "rotateAuthorizationScope" in auth
    assert "clearApiDedupeCache()" in auth
    for src in [clients, wallet, brands, home]:
        assert "authorizationScope" in src
    assert "activeAuthorizationScopeRef.current !== requestedScope" in wallet
    assert "dataOwnerScope === authorizationScope" in wallet


def test_generic_get_dedupe_is_pending_only_and_mutations_invalidate():
    api = read("frontend/src/lib/api.ts")
    v35 = read("frontend/src/lib/v35w2Api.ts")
    assert "PENDING_GET_REQUESTS" in api
    assert "function abortPendingByTags" in api
    assert "invalidateApiResourcesForMutation(urlStr)" in api
    assert "export function clearApiDedupeCache" in api
    assert "READ_CACHE_TTL" not in api
    assert "DEDUPE_WINDOW_MS" not in api
    assert "readInFlight" in v35
    assert "readCache" not in v35
    assert "READ_CACHE_TTL" not in v35
    assert "omnirank-api-mutated" in v35
    assert "omnirank-authorization-changed" in v35
    assert "const activeReadControllers = new Map" in v35
    assert "function clearReadFlights(tags?: Set<string>)" in v35
    assert "if (tags && ![...readTags].some(tag => tags.has(tag))) continue" in v35
    assert "omnirank-authorization-changed', () => clearReadFlights()" in v35


def test_portal_dashboard_does_not_mount_authenticated_notification_center():
    src = read("frontend/src/pages/Portal/PortalDashboard.tsx")
    assert "NotificationCenter" not in src
