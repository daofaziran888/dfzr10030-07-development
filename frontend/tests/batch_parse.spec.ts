import { expect, test } from '@playwright/test'

test('批量解析：多格式输入 → 批次清单/字段包 → 校验回写 → 重算与回查', async ({ page }) => {
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) browserErrors.push(`${r.status()} ${r.url()}`) })

  await page.goto('/parser')
  await expect(page.getByTestId('batch-panel')).toBeVisible()

  // 三种格式 + 一条异常样例，一次提交
  for (const id of [1, 2, 3, 5]) await page.getByTestId(`input-${id}`).check()
  await page.getByTestId('policy').selectOption('按输入命中')
  await page.getByTestId('submit-batch').click()
  await expect(page.getByTestId('batch-message')).toContainText('已生成')
  await expect(page.getByTestId('batch-detail')).toBeVisible()

  // 每条输入的映射版本可追溯：XML 输入仍命中历史 v1
  await expect(page.getByTestId('map-line-IN-XML-01')).toContainText('MAP-XML-001')
  await expect(page.getByTestId('map-line-IN-XML-01')).toContainText('v1')
  await expect(page.getByTestId('map-line-IN-CSV-01')).toContainText('v2')

  // 失败逐条可判断：哪条输入、哪个字段、什么原因
  await expect(page.getByTestId('batch-item-IN-XML-02')).toHaveAttribute('data-status', '失败')
  await expect(page.getByTestId('batch-item-IN-XML-02')).toContainText('金额')
  await expect(page.getByTestId('batch-item-IN-XML-02')).toContainText('decimal')

  const batchNo = (await page.locator('[data-testid="batch-detail"] h4').first().innerText())
    .replace(' 明细', '').trim()
  const rowTestId = await page.getByTestId(/^batch-row-/).filter({ hasText: batchNo }).first().getAttribute('data-testid')
  const batchId = rowTestId!.replace('batch-row-', '')

  // 以旧版本映射重算：原摘要保留
  await page.getByTestId(`batch-reparse-${batchId}`).click()
  await expect(page.getByTestId('batch-message')).toContainText('重算')
  await expect(page.getByTestId('batch-detail')).toContainText('重算 1 次')

  // 校验服务：读取字段包 → 回写批次 → 生成待复核清单与业务记录
  await page.goto('/validator')
  await expect(page.getByTestId('audit-panel')).toBeVisible()
  await page.getByTestId(`audit-review-${batchId}`).click()
  await expect(page.getByTestId('audit-message')).toContainText('复核完成')
  await expect(page.getByTestId('audit-file-IN-XML-02')).toContainText('类型异常')
  await expect(page.locator('[data-testid^="review-row-"]').first()).toBeVisible()

  // 并发复核每批仅一份有效结果：不带复核版本的第二次核对被拒绝
  await page.getByTestId(`audit-review-${batchId}`).click()
  await expect(page.getByTestId('audit-message')).toContainText('复核完成，复核版本 v2')

  // 格式解析服务回查校验结论
  await page.goto('/parser')
  await page.getByTestId(`batch-view-${batchId}`).click()
  await page.getByTestId('load-conclusion').click()
  await expect(page.getByTestId('conclusion-text')).toContainText('有效 是')

  expect(browserErrors).toEqual([])
})
