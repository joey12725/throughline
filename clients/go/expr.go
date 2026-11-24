package throughline

import (
	"fmt"
	"regexp"
	"strings"
)

var (
	identRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	typeRe  = regexp.MustCompile(`^[a-z][a-z0-9_ ]*(\[\])?$`)
)

func quoteIdent(name string) (string, error) {
	if name == "*" {
		return "*", nil
	}
	parts := strings.Split(name, ".")
	if len(parts) > 2 {
		return "", fmt.Errorf("invalid column identifier: %q", name)
	}
	out := make([]string, len(parts))
	for i, part := range parts {
		if part == "*" && i == len(parts)-1 {
			out[i] = "*"
			continue
		}
		if !identRe.MatchString(part) {
			return "", fmt.Errorf("invalid SQL identifier: %q", part)
		}
		out[i] = `"` + part + `"`
	}
	return strings.Join(out, "."), nil
}

type paramCollector struct {
	values []any
}

func (p *paramCollector) add(v any) string {
	p.values = append(p.values, v)
	return fmt.Sprintf("$%d", len(p.values))
}

// Expr is a composable Snowpark-style SQL expression.
type Expr struct {
	compileFn func(*paramCollector) (string, error)
	alias     string
	err       error
}

// OrderExpr represents an ORDER BY item.
type OrderExpr struct {
	expr      Expr
	desc      bool
	nullsLast *bool
}

func (e Expr) compile(p *paramCollector) (string, error) {
	if e.err != nil {
		return "", e.err
	}
	return e.compileFn(p)
}

func (e Expr) compileSelect(p *paramCollector) (string, error) {
	sql, err := e.compile(p)
	if err != nil {
		return "", err
	}
	if e.alias != "" {
		q, err := quoteIdent(e.alias)
		if err != nil {
			return "", err
		}
		return sql + " AS " + q, nil
	}
	return sql, nil
}

func (o OrderExpr) compile(p *paramCollector) (string, error) {
	sql, err := o.expr.compile(p)
	if err != nil {
		return "", err
	}
	if o.desc {
		sql += " DESC"
	} else {
		sql += " ASC"
	}
	if o.nullsLast != nil {
		if *o.nullsLast {
			sql += " NULLS LAST"
		} else {
			sql += " NULLS FIRST"
		}
	}
	return sql, nil
}

// Col references a column (e.g. Col("happened_at") or Col("m.channel")).
func Col(name string) Expr {
	q, err := quoteIdent(name)
	return Expr{
		err: err,
		compileFn: func(_ *paramCollector) (string, error) {
			return q, nil
		},
	}
}

// Lit binds a Go value as a $N positional SQL parameter.
func Lit(value any) Expr {
	if value == nil {
		return Expr{compileFn: func(_ *paramCollector) (string, error) { return "NULL", nil }}
	}
	return Expr{compileFn: func(p *paramCollector) (string, error) { return p.add(value), nil }}
}

// Raw creates a raw SQL expression fragment, binding '?' placeholders to params.
func Raw(sqlExpr string, params ...any) Expr {
	return Expr{
		compileFn: func(p *paramCollector) (string, error) {
			if len(params) == 0 {
				return "(" + sqlExpr + ")", nil
			}
			parts := strings.Split(sqlExpr, "?")
			if len(parts)-1 != len(params) {
				return "", fmt.Errorf("Raw expected %d params for '?' placeholders, got %d", len(parts)-1, len(params))
			}
			var b strings.Builder
			b.WriteByte('(')
			b.WriteString(parts[0])
			for i, val := range params {
				b.WriteString(p.add(val))
				b.WriteString(parts[i+1])
			}
			b.WriteByte(')')
			return b.String(), nil
		},
	}
}

func asExpr(v any) Expr {
	if e, ok := v.(Expr); ok {
		return e
	}
	return Lit(v)
}

func escapeLike(s string) string {
	s = strings.ReplaceAll(s, `\`, `\\`)
	s = strings.ReplaceAll(s, `%`, `\%`)
	s = strings.ReplaceAll(s, `_`, `\_`)
	return s
}

func (e Expr) bin(op string, rhs any) Expr {
	right := asExpr(rhs)
	return Expr{
		compileFn: func(p *paramCollector) (string, error) {
			l, err := e.compile(p)
			if err != nil {
				return "", err
			}
			r, err := right.compile(p)
			if err != nil {
				return "", err
			}
			return fmt.Sprintf("(%s %s %s)", l, op, r), nil
		},
	}
}

func (e Expr) As(alias string) Expr {
	if _, err := quoteIdent(alias); err != nil {
		return Expr{err: err}
	}
	e.alias = alias
	return e
}

func (e Expr) Asc() OrderExpr  { return OrderExpr{expr: e, desc: false} }
func (e Expr) Desc() OrderExpr { return OrderExpr{expr: e, desc: true} }

func (e Expr) Eq(v any) Expr {
	if v == nil {
		return e.IsNull()
	}
	return e.bin("=", v)
}

func (e Expr) Neq(v any) Expr {
	if v == nil {
		return e.IsNotNull()
	}
	return e.bin("<>", v)
}

func (e Expr) Lt(v any) Expr   { return e.bin("<", v) }
func (e Expr) Lte(v any) Expr  { return e.bin("<=", v) }
func (e Expr) Gt(v any) Expr   { return e.bin(">", v) }
func (e Expr) Gte(v any) Expr  { return e.bin(">=", v) }
func (e Expr) And(o Expr) Expr { return e.bin("AND", o) }
func (e Expr) Or(o Expr) Expr  { return e.bin("OR", o) }

func (e Expr) Not() Expr {
	return Expr{compileFn: func(p *paramCollector) (string, error) {
		s, err := e.compile(p)
		if err != nil {
			return "", err
		}
		return "(NOT " + s + ")", nil
	}}
}

func (e Expr) IsNull() Expr {
	return Expr{compileFn: func(p *paramCollector) (string, error) {
		s, err := e.compile(p)
		if err != nil {
			return "", err
		}
		return "(" + s + " IS NULL)", nil
	}}
}

func (e Expr) IsNotNull() Expr {
	return Expr{compileFn: func(p *paramCollector) (string, error) {
		s, err := e.compile(p)
		if err != nil {
			return "", err
		}
		return "(" + s + " IS NOT NULL)", nil
	}}
}

func (e Expr) In(values ...any) Expr {
	if len(values) == 0 {
		return Expr{compileFn: func(_ *paramCollector) (string, error) { return "FALSE", nil }}
	}
	return Expr{compileFn: func(p *paramCollector) (string, error) {
		lhs, err := e.compile(p)
		if err != nil {
			return "", err
		}
		ph := make([]string, len(values))
		for i, v := range values {
			ph[i] = p.add(v)
		}
		return fmt.Sprintf("(%s IN (%s))", lhs, strings.Join(ph, ", ")), nil
	}}
}

func (e Expr) Between(lo, hi any) Expr {
	low, high := asExpr(lo), asExpr(hi)
	return Expr{compileFn: func(p *paramCollector) (string, error) {
		lhs, err := e.compile(p)
		if err != nil {
			return "", err
		}
		l, err := low.compile(p)
		if err != nil {
			return "", err
		}
		h, err := high.compile(p)
		if err != nil {
			return "", err
		}
		return fmt.Sprintf("(%s BETWEEN %s AND %s)", lhs, l, h), nil
	}}
}

func (e Expr) Like(pattern string) Expr     { return e.bin("LIKE", pattern) }
func (e Expr) ILike(pattern string) Expr    { return e.bin("ILIKE", pattern) }
func (e Expr) Contains(sub string) Expr     { return e.ILike("%" + escapeLike(sub) + "%") }
func (e Expr) StartsWith(pre string) Expr   { return e.ILike(escapeLike(pre) + "%") }
func (e Expr) EndsWith(suf string) Expr     { return e.ILike("%" + escapeLike(suf)) }
func (e Expr) JSON(key string) Expr         { return e.bin("->>", key) }
func (e Expr) JSONObj(key string) Expr      { return e.bin("->", key) }

func (e Expr) Cast(pgType string) Expr {
	cleaned := strings.Join(strings.Fields(strings.ToLower(pgType)), " ")
	if !typeRe.MatchString(cleaned) || strings.HasPrefix(cleaned, "reg") {
		return Expr{err: fmt.Errorf("unsupported cast type: %q", pgType)}
	}
	return Expr{compileFn: func(p *paramCollector) (string, error) {
		s, err := e.compile(p)
		if err != nil {
			return "", err
		}
		return fmt.Sprintf("CAST(%s AS %s)", s, cleaned), nil
	}}
}

// --- Aggregate & scalar SQL function helpers ---

func fnCall(name string, args ...Expr) Expr {
	return Expr{compileFn: func(p *paramCollector) (string, error) {
		parts := make([]string, len(args))
		for i, a := range args {
			s, err := a.compile(p)
			if err != nil {
				return "", err
			}
			parts[i] = s
		}
		return fmt.Sprintf("%s(%s)", name, strings.Join(parts, ", ")), nil
	}}
}

func Count(column string) Expr {
	if column == "" || column == "*" {
		return Expr{compileFn: func(_ *paramCollector) (string, error) { return "count(*)", nil }}
	}
	return fnCall("count", Col(column))
}

func CountDistinct(column string) Expr {
	c := Col(column)
	return Expr{compileFn: func(p *paramCollector) (string, error) {
		s, err := c.compile(p)
		if err != nil {
			return "", err
		}
		return "count(DISTINCT " + s + ")", nil
	}}
}

func Sum(colName string) Expr               { return fnCall("sum", Col(colName)) }
func Avg(colName string) Expr               { return fnCall("avg", Col(colName)) }
func Min(colName string) Expr               { return fnCall("min", Col(colName)) }
func Max(colName string) Expr               { return fnCall("max", Col(colName)) }
func Lower(colName string) Expr             { return fnCall("lower", Col(colName)) }
func Upper(colName string) Expr             { return fnCall("upper", Col(colName)) }
func DateTrunc(unit, colName string) Expr   { return fnCall("date_trunc", Lit(unit), Col(colName)) }
func Coalesce(exprs ...Expr) Expr           { return fnCall("coalesce", exprs...) }
func StringAgg(colName, delim string) Expr  { return fnCall("string_agg", Col(colName), Lit(delim)) }
func ArrayAgg(colName string) Expr          { return fnCall("array_agg", Col(colName)) }
func Now() Expr                             { return fnCall("now") }
