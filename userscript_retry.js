  // OCS answer retry patch begin
  async function requestAnswerWrapperWithRetry(url, options) {
    const { retry_count, retry_interval_seconds } = AnswerWrapperHandlerConfig;
    for (let attempt = 0; attempt <= retry_count; attempt++) {
      let timeout;
      try {
        const response = await Promise.race([
          request(url, { ...options, ocsQuestionBankRequest: true }),
          new Promise((_, reject) => {
            timeout = setTimeout(() => {
              reject(new Error("题库请求超时，可能是题库问题，或者请检查网络或者重试。"));
            }, AnswerWrapperHandlerConfig.timeout_seconds * 1e3);
          })
        ]);
        if (response === void 0) {
          throw new Error("题库连接失败，响应为空");
        }
        return response;
      } catch (error) {
        if (attempt === retry_count) {
          throw error;
        }
        console.warn(`题库连接失败，${retry_interval_seconds} 秒后进行第 ${attempt + 1}/${retry_count} 次重试`, error);
      } finally {
        clearTimeout(timeout);
      }
      await new Promise((resolve) => setTimeout(resolve, retry_interval_seconds * 1e3));
    }
  }
  // OCS answer retry patch end
