export default class StrictPerformanceReporter {
  expected = 0;
  completed = 0;
  skipped = 0;

  onBegin(_config, suite) {
    this.expected = suite.allTests().length;
  }

  onTestEnd(_test, result) {
    this.completed += 1;
    if (result.status === 'skipped') this.skipped += 1;
  }

  onEnd(result) {
    if (process.argv.includes('--list')) return { status: 'passed' };
    if (result.status !== 'passed' || this.completed !== this.expected || this.skipped !== 0) {
      return { status: 'failed' };
    }
    return { status: 'passed' };
  }
}
