// Public surface of the lens engine. Pure TypeScript: no React, no DOM, no fetch.

export * from "./types";
export {
  EXTENDED,
  EXTENDED_BUILTINS,
  GLOBAL_PARAMS,
  LensSqlError,
  NARROW,
  NARROW_BUILTINS,
  bucketExpr,
  builtinProbes,
  capabilities,
  channelView,
  channelViewName,
  chooseGrain,
  compileView,
  freeAlias,
  grainSeconds,
  isGrain,
  lit,
  localDayExpr,
  numericAgg,
  paramOf,
  quoteIdent,
  renderParam,
  sampleModulo,
  scanParams,
  scopeFilter,
  sqlFns,
  substituteParams,
  wrapStatement,
} from "./sql";
export type { Capabilities, CompiledView, LateralElements, ParamRef, Scope, SqlFns, Substitution, WindowSpec } from "./sql";
export {
  assembleChannelProfile,
  cacheKey,
  declaredProfile,
  profileChannel,
  profilePlan,
  profileResult,
} from "./profile";
export type { ProfileOptions, ProfilePlan, ProfileStatement } from "./profile";
export { channelFamilies, fieldFamilies, inferRoles, isCategory, isMeasure, isPlumbing, mineDomain, mineUnit, partsOfWhole } from "./roles";
export { archetypeScore, scoreArchetypes, shapeFacts } from "./archetypes";
export { findLevels } from "./levels";
export type { Level } from "./levels";
export type { ShapeFacts } from "./archetypes";
export { LENSES, LENS_CATALOG, LENS_LIST, REPLY_BINS, readerVariant } from "./lenses/index";
export type { LensCatalogEntry, LensDef, ReaderVariant } from "./lenses/index";
export { analyzeChannel, analyzeResult, lensesForChannel, lensesForResult } from "./engine";
export type { ChannelAnalysis, EngineOptions, ResultAnalysis } from "./engine";
export { applyLensOverrides, applyRoleOverrides, candidateKey, candidateKeys, normalizeOverrides } from "./overrides";
export { describeResult, questionFor, startersForChannel } from "./describe";
export type { Starter } from "./describe";
