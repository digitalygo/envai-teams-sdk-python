import picomatch from 'picomatch';

function requireDefaultOptions(options) {
  if (options !== undefined) {
    throw new TypeError('Release matching options are unsupported; review the locked consumers before extending this adapter');
  }
}

function match(list, patterns, options) {
  requireDefaultOptions(options);
  const candidates = [].concat(list);
  const expressions = [].concat(patterns);
  const omitted = new Set();
  const included = new Set();
  const visited = new Set();
  let negations = 0;

  for (const expression of expressions) {
    const matcher = picomatch(String(expression), {
      onResult: (result) => visited.add(result.output),
    }, true);
    const negated = matcher.state.negated || matcher.state.negatedExtglob;

    if (negated) {
      negations++;
    }

    for (const candidate of candidates) {
      const result = matcher(candidate, true);
      const matched = negated ? !result.isMatch : result.isMatch;

      if (!matched) {
        continue;
      }

      if (negated) {
        omitted.add(result.output);
      } else {
        omitted.delete(result.output);
        included.add(result.output);
      }
    }
  }

  const result = negations === expressions.length ? visited : included;

  return [...result].filter((candidate) => !omitted.has(candidate));
}

match.isMatch = (value, patterns, options) => {
  requireDefaultOptions(options);

  return picomatch(patterns)(value);
};

export default new Proxy(match, {
  get(target, property, receiver) {
    if (typeof property === 'symbol' || Reflect.has(target, property)) {
      return Reflect.get(target, property, receiver);
    }

    throw new TypeError(`Unsupported release matching API: ${String(property)}`);
  },
});
