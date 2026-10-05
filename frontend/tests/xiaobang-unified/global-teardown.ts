/** Playwright 的 globalTeardown 必须是**默认导出**,所以单独一个薄壳。 */
import { globalTeardown } from './server-guard'

export default globalTeardown
