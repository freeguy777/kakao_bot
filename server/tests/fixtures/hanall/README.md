# HanAll Collector Fixtures

이 디렉터리는 한올 브리핑 collector/parser 회귀 테스트용 sanitized fixture를 source별로 보관한다.

## Refresh

1. live smoke test로 source별 실제 응답을 확인한다.
2. 응답 본문에서 API key, `api-key`, `serviceKey`, `token`, query string, 내부 식별자, 과도한 본문 길이를 제거한다.
3. source 디렉터리에 `normal`, `empty`, `variant`, `http_*` 성격으로 저장한다.
4. parser 회귀 테스트에서 새 fixture를 바로 참조하도록 케이스를 추가한다.

## Known Fragile Fields

- `sec_api`: `description/title/documentFormatFiles[*].description`, `filedAt/acceptedAt/filingDate`
- `opendart`: `corpCode.xml` zip 내부 XML 구조, `report_nm/rcept_dt/rcept_no`
- `openfda`: endpoint마다 `purpose`, `reason_for_recall`, `submission_type`, `openfda.*`
- `clinicaltrials`: `organization.fullName` 부재 시 `leadSponsor.name`, 날짜 struct vs plain string
- `cris` / `mfds`: `response.body.items.item` 단건 dict vs list, 승인/비승인 dataset 편차
- `ncbi`: `authors[0].name` vs `sortfirstauthor`, `pubdate` 형식 편차
- `europe_pmc`: `resultList.result` dict vs list, DOI 부재 시 article URL 조합
- `crossref`: `title` string vs list, `issued/published-online/created` date-parts
- `biorxiv` / `medrxiv`: `authors` vs `author_corresponding`, 날짜 slash format
