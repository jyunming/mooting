import time


def retry(fn, attempts: int = 3, base_delay: float = 0.5,
          retry_on: tuple = (ConnectionError, TimeoutError)):
    """Call fn() until it succeeds, up to `attempts` times.

    Waits base_delay, then twice that, and so on between attempts. Errors not
    in `retry_on` propagate at once. If every attempt fails, the last error is
    raised.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last = None
    for n in range(attempts):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            if n < attempts - 1:
                time.sleep(base_delay * 2 ** n)
    return last
