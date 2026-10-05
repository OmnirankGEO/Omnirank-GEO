/** Presentation only. Prices, qualification and publication stay server-owned. */
export function positiveId(value: unknown): number | null {
    const raw = String(value ?? '');
    if (!/^[1-9]\d*$/.test(raw)) return null;
    const id = Number(raw);
    return Number.isSafeInteger(id) ? id : null;
}

export function publicationLayout(mode: string, type: string, mobileStep: string) {
    const standalone = mode === 'proxy' && (type === 'svideo' || type === 'imagenote');
    return {
        // [WO_273] 浏览器插件自助发布已退役:文章栏只属于代发(自助那一档不再是模式)。
        articlePane: mode === 'proxy' && !standalone,
        mediaVisible: mobileStep === 'media' || standalone,
        articleCart: mode === 'proxy' && !standalone,
    };
}

export function imageNotePublishHref(postId: number, brandId: number | null): string {
    const q = new URLSearchParams({ media_type: 'svideo', content_type: 'imagenote', geo_post_id: String(postId) });
    if (brandId) q.set('brand_id', String(brandId));
    return `/publish?${q.toString()}`;
}

/** Old image-note links and post handoffs share the short-video publishing lane. */
export function publicationEntry(q: URLSearchParams): { type: 'article' | 'wemedia' | 'svideo'; content: 'imagenote' | 'video' } {
    const raw = q.get('media_type');
    const hasPost = positiveId(q.get('geo_post_id')) !== null;
    return {
        type: raw === 'imagenote' || raw === 'svideo' || hasPost ? 'svideo' : raw === 'wemedia' ? 'wemedia' : 'article',
        content: raw === 'imagenote' || hasPost || q.get('content_type') === 'imagenote' ? 'imagenote' : 'video',
    };
}

/** Persist a late receipt for its original work without changing the current UI. */
export function deliverPublishCompletion({ commandKey, commandId, remember, isCurrent, onCurrent }: {
    commandKey: string;
    commandId: string;
    remember: (key: string, value: string) => void;
    isCurrent: () => boolean;
    onCurrent: () => void;
}): boolean {
    try { remember(commandKey, commandId); } catch { /* Storage is an optional resume hint. */ }
    if (!isCurrent()) return false;
    onCurrent();
    return true;
}

/** Existing work may revisit production or editing without creating another job. */
export function existingWorkStep(step: number): { stage: 2 | 3 } | { href: string } {
    return step === 2 || step === 3 ? { stage: step } : { href: '/writing/image-note' };
}

export interface ClientHandoff {
    key: string;
    brandId: number;
    phase: 'pending' | 'observed' | 'released';
}

/** A requested context switch isn't an observed switch (effects may run twice). */
export function advanceClientHandoff(previous: ClientHandoff | null, key: string,
    targetBrandId: number | null, currentBrandId: number | null): {
        state: ClientHandoff | null; action: 'none' | 'switch' | 'clear';
    } {
    if (!key) return { state: null, action: 'none' };
    if (!targetBrandId) return { state: previous?.key === key ? previous : null, action: 'none' };
    if (!previous || previous.key !== key || previous.brandId !== targetBrandId) {
        const observed = currentBrandId === targetBrandId;
        return { state: { key, brandId: targetBrandId, phase: observed ? 'observed' : 'pending' }, action: observed ? 'none' : 'switch' };
    }
    if (previous.phase === 'released') return { state: previous, action: 'none' };
    if (previous.phase === 'pending') {
        return { state: currentBrandId === targetBrandId ? { ...previous, phase: 'observed' } : previous, action: 'none' };
    }
    return currentBrandId === targetBrandId ? { state: previous, action: 'none' }
        : { state: { ...previous, phase: 'released' }, action: 'clear' };
}

export function scopedPost(post: unknown, brandId: number | null): Record<string, unknown> | null {
    if (!post || typeof post !== 'object') return null;
    const value = post as Record<string, unknown>;
    return positiveId(value.id) && positiveId(value.brand_id) === brandId ? value : null;
}

/** A handoff opens once; polling must not override a later manual choice (including closed). */
export function expandedAfterHandoff(previousScope: string, nextScope: string,
    currentOpenId: number | null, handedOffPostId: unknown): number | null {
    return previousScope === nextScope ? currentOpenId : positiveId(handedOffPostId);
}

export function sameCopy(a: { title: string; body: string; tags: string[] },
    b: { title: string; body: string; tags: string[] }): boolean {
    return a.title === b.title && a.body === b.body && JSON.stringify(a.tags) === JSON.stringify(b.tags);
}

export function matchesSavedRevision(receipt: unknown, latest: unknown): boolean {
    if (!receipt || !latest || typeof receipt !== 'object' || typeof latest !== 'object') return false;
    const a = receipt as Record<string, unknown>, b = latest as Record<string, unknown>;
    if (!Object.prototype.hasOwnProperty.call(a, 'active_revision_id')) return false;
    if (a.active_revision_id === null) return b.active_revision_id === null;
    const expected = positiveId(a.active_revision_id);
    return expected !== null && expected === positiveId(b.active_revision_id);
}
