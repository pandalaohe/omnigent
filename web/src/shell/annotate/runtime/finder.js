// @medv/finder 4.0.2 (MIT) — https://github.com/antonmedv/finder
// Copyright (c) 2018 Anton Medvedev. Full licence text: THIRD_PARTY_NOTICES.md.

var acceptedAttrNames = new Set(["role", "name", "aria-label", "rel", "href"]);

/** Check if attribute name and value are word-like. */
function attr(name, value) {
  var nameIsOk = acceptedAttrNames.has(name);
  nameIsOk = nameIsOk || (name.startsWith("data-") && wordLike(name));

  var valueIsOk = wordLike(value) && value.length < 100;
  valueIsOk = valueIsOk || (value.startsWith("#") && wordLike(value.slice(1)));

  return nameIsOk && valueIsOk;
}

/** Check if id name is word-like. */
function idName(name) {
  return wordLike(name);
}

/** Check if class name is word-like. */
function className(name) {
  return wordLike(name);
}

/** Check if tag name is word-like. */
function tagName() {
  return true;
}

/** Finds unique CSS selectors for the given element. */
function finder(input, options) {
  if (input.nodeType !== Node.ELEMENT_NODE) {
    throw new Error("Can't generate CSS selector for non-element node type.");
  }
  if (input.tagName.toLowerCase() === "html") {
    return "html";
  }
  var defaults = {
    root: document.body,
    idName: idName,
    className: className,
    tagName: tagName,
    attr: attr,
    timeoutMs: 1000,
    seedMinLength: 3,
    optimizedMinLength: 2,
    maxNumberOfPathChecks: Infinity,
  };

  var startTime = new Date();
  var config = Object.assign({}, defaults, options);
  var rootDocument = findRootDocument(config.root, defaults, input);

  var foundPath;
  var count = 0;
  for (var candidate of search(input, config, rootDocument)) {
    var elapsedTimeMs = new Date().getTime() - startTime.getTime();
    if (elapsedTimeMs > config.timeoutMs || count >= config.maxNumberOfPathChecks) {
      var fPath = fallback(input, rootDocument);
      if (!fPath) {
        throw new Error("Timeout: Can't find a unique selector after " + config.timeoutMs + "ms");
      }
      return selector(fPath);
    }
    count++;
    if (unique(candidate, rootDocument)) {
      foundPath = candidate;
      break;
    }
  }

  if (!foundPath) {
    throw new Error("Selector was not found.");
  }

  var optimized = [...optimize(foundPath, input, config, rootDocument, startTime)];
  optimized.sort(byPenalty);
  if (optimized.length > 0) {
    return selector(optimized[0]);
  }
  return selector(foundPath);
}

function* search(input, config, rootDocument) {
  var stack = [];
  var paths = [];
  var current = input;
  var i = 0;
  while (current && current !== rootDocument) {
    var level = tie(current, config);
    for (var node of level) {
      node.level = i;
    }
    stack.push(level);
    current = current.parentElement;
    i++;

    paths.push(...combinations(stack));

    if (i >= config.seedMinLength) {
      paths.sort(byPenalty);
      for (var candidate of paths) {
        yield candidate;
      }
      paths = [];
    }
  }

  paths.sort(byPenalty);
  for (var tail of paths) {
    yield tail;
  }
}

function wordLike(name) {
  if (/^[a-z-]{3,}$/i.test(name)) {
    var words = name.split(/-|[A-Z]/);
    for (var word of words) {
      if (word.length <= 2) {
        return false;
      }
      if (/[^aeiou]{4,}/i.test(word)) {
        return false;
      }
    }
    return true;
  }
  return false;
}

function tie(element, config) {
  var level = [];

  var elementId = element.getAttribute("id");
  if (elementId && config.idName(elementId)) {
    level.push({
      name: "#" + CSS.escape(elementId),
      penalty: 0,
    });
  }

  for (var i = 0; i < element.classList.length; i++) {
    var name = element.classList[i];
    if (config.className(name)) {
      level.push({
        name: "." + CSS.escape(name),
        penalty: 1,
      });
    }
  }

  for (var j = 0; j < element.attributes.length; j++) {
    var attribute = element.attributes[j];
    if (config.attr(attribute.name, attribute.value)) {
      level.push({
        name: `[${CSS.escape(attribute.name)}="${CSS.escape(attribute.value)}"]`,
        penalty: 2,
      });
    }
  }

  var tag = element.tagName.toLowerCase();
  if (config.tagName(tag)) {
    level.push({
      name: tag,
      penalty: 5,
    });

    var index = indexOf(element, tag);
    if (index !== undefined) {
      level.push({
        name: nthOfType(tag, index),
        penalty: 10,
      });
    }
  }

  var nth = indexOf(element);
  if (nth !== undefined) {
    level.push({
      name: nthChild(tag, nth),
      penalty: 50,
    });
  }

  return level;
}

function selector(path) {
  var node = path[0];
  var query = node.name;
  for (var i = 1; i < path.length; i++) {
    var level = path[i].level || 0;
    if (node.level === level - 1) {
      query = `${path[i].name} > ${query}`;
    } else {
      query = `${path[i].name} ${query}`;
    }
    node = path[i];
  }
  return query;
}

function penalty(path) {
  return path.map((node) => node.penalty).reduce((acc, i) => acc + i, 0);
}

function byPenalty(a, b) {
  return penalty(a) - penalty(b);
}

function indexOf(input, tagFilter) {
  var parent = input.parentNode;
  if (!parent) {
    return undefined;
  }
  var child = parent.firstChild;
  if (!child) {
    return undefined;
  }
  var i = 0;
  while (child) {
    if (
      child.nodeType === Node.ELEMENT_NODE &&
      (tagFilter === undefined || child.tagName.toLowerCase() === tagFilter)
    ) {
      i++;
    }
    if (child === input) {
      break;
    }
    child = child.nextSibling;
  }
  return i;
}

function fallback(input, rootDocument) {
  var i = 0;
  var current = input;
  var path = [];
  while (current && current !== rootDocument) {
    var currentTag = current.tagName.toLowerCase();
    var index = indexOf(current, currentTag);
    if (index === undefined) {
      return;
    }
    path.push({
      name: nthOfType(currentTag, index),
      penalty: NaN,
      level: i,
    });
    current = current.parentElement;
    i++;
  }
  if (unique(path, rootDocument)) {
    return path;
  }
}

function nthChild(tag, index) {
  if (tag === "html") {
    return "html";
  }
  return `${tag}:nth-child(${index})`;
}

function nthOfType(tag, index) {
  if (tag === "html") {
    return "html";
  }
  return `${tag}:nth-of-type(${index})`;
}

function* combinations(stack, path = []) {
  if (stack.length > 0) {
    for (var node of stack[0]) {
      yield* combinations(stack.slice(1, stack.length), path.concat(node));
    }
  } else {
    yield path;
  }
}

function findRootDocument(rootNode, defaults, input) {
  var shadowRoot = input.getRootNode && input.getRootNode();
  if (shadowRoot && shadowRoot.constructor && shadowRoot.constructor.name === "ShadowRoot") {
    return shadowRoot;
  }
  if (rootNode.nodeType === Node.DOCUMENT_NODE) {
    return rootNode;
  }
  if (rootNode === defaults.root) {
    return rootNode.ownerDocument;
  }
  return rootNode;
}

function unique(path, rootDocument) {
  var css = selector(path);
  switch (rootDocument.querySelectorAll(css).length) {
    case 0:
      throw new Error(`Can't select any node with this selector: ${css}`);
    case 1:
      return true;
    default:
      return false;
  }
}

function* optimize(path, input, config, rootDocument, startTime) {
  if (path.length > 2 && path.length > config.optimizedMinLength) {
    for (var i = 1; i < path.length - 1; i++) {
      var elapsedTimeMs = new Date().getTime() - startTime.getTime();
      if (elapsedTimeMs > config.timeoutMs) {
        return;
      }
      var newPath = [...path];
      newPath.splice(i, 1);
      if (
        unique(newPath, rootDocument) &&
        rootDocument.querySelector(selector(newPath)) === input
      ) {
        yield newPath;
        yield* optimize(newPath, input, config, rootDocument, startTime);
      }
    }
  }
}

ns.finder = finder;
ns.finderWordLike = wordLike;
ns.finderAttr = attr;
