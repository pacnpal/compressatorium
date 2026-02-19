const KEY = '__igirPreselectedInput';

export function setIgirPreselectedInput(path) {
    globalThis[KEY] = path;
}

export function consumeIgirPreselectedInput() {
    const value = globalThis[KEY];
    if (typeof value === 'string' && value.length > 0) {
        delete globalThis[KEY];
        return value;
    }
    delete globalThis[KEY];
    return null;
}
