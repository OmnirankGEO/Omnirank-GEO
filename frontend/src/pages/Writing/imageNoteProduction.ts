import type { TopicRow } from './imageNoteTopics';

export interface ProductionSettings { cardCount: number; styleKey: string; aspectRatio: string; industry: string }
export interface ProductionQuote { total_points: number; lines: Array<{ price_fingerprint: string }> }

export function productionLines(rows: TopicRow[], settings: ProductionSettings) {
    return rows.map(row => ({ keyword: row.keyword, city: row.city, card_count: settings.cardCount }));
}

export function productionBatch(brandId: number, rows: TopicRow[], settings: ProductionSettings,
    quote: ProductionQuote, requestId: string) {
    if (rows.length < 1 || rows.length > 20 || quote.lines.length !== rows.length
        || !quote.lines.every(line => typeof line.price_fingerprint === 'string' && line.price_fingerprint.length > 0)) {
        /* 这句是写给人看的,上屏原样用 —— 名字即 imageNoteTopics.USER_ERROR_NAME
           (这里不能值导入它:判据按 data: URL 单独加载本模块,相对 import 解析不了) */
        throw Object.assign(new Error('制作清单或报价已变化，请重新确认。'), { name: 'ImageNoteUserError' });
    }
    return { request_id: requestId, items: rows.map((row, i) => ({
        ...productionLines([row], settings)[0], brand_id: brandId,
        ...(row.fromKeyword ? (row.topicId ? { topic_id: row.topicId } : {}) : { topic_id: row.id }),
        industry_key: settings.industry, style_key: settings.styleKey || row.styleKey,
        aspect_ratio: settings.aspectRatio, confirmed_keyword_id: row.confirmedKeywordId ?? null,
        expected_price_fingerprint: quote.lines[i].price_fingerprint,
    })) };
}
