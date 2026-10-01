#!/usr/bin/perl
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
# Unified front door for the maintainer container: one process on the one
# externally-published port, routing by path to the mail archive (/lore),
# cgit (/cgit), git smart/dumb-HTTP clones at the root (mirroring upstream's
# own repo paths, the same way git.kernel.org itself is laid out), the MCP
# server (/mcp), and falling through to the Python setup/dashboard app for
# everything else.
#
# Run under public-inbox-netd's own `psgi=` listener option rather than a
# second HTTP server -- PublicInbox::WWW/::Cgit/::GitHTTPBackend already do
# the subpath-aware routing and CGI-spawning this would otherwise mean
# reimplementing by hand, and it means no separate CGI server (uwsgi or
# the like) is needed just to run cgit and git-http-backend.

use strict;
use warnings;
use v5.12;
use Plack::Builder;
use Plack::Util;
use HTTP::Tiny;
# Monotonic, so a clock step mid-request cannot stretch or void a deadline.
use Time::HiRes qw(clock_gettime CLOCK_MONOTONIC);
use POSIX qw(sigprocmask SIG_UNBLOCK SIG_SETMASK SIGALRM);
use PublicInbox::Config;
use PublicInbox::WWW;
use PublicInbox::Cgit;
use PublicInbox::Git;
use PublicInbox::GitHTTPBackend;
use PublicInbox::WwwStatic;

# The objects a psgi= script captures at load time are only refreshed by
# public-inbox-netd killing and respawning its workers on SIGHUP, which
# makes a freshly tracked subsystem's appearance in /lore and /cgit
# depend on the daemon's respawn semantics and on the HUP actually
# arriving. Re-read it here instead, whenever write_publicinbox_config
# (setup/app.py) has touched the file since this process last loaded it.
# That costs one stat per request and is true in whichever worker takes
# the request, so it holds at any worker count -- including -W0, which
# this ran under until WEB_WORKERS existed and which has no workers to
# respawn at all.
my ($pi_cfg, $www, $cgit, $pi_cfg_mtime);
sub load_pi_config {
	my $mtime = (stat($ENV{PI_CONFIG}))[9] // 0;
	return if defined($pi_cfg_mtime) && $mtime == $pi_cfg_mtime;
	# PublicInbox::Config->new caches by absolute path in its own
	# $DEDUPE global with no mtime check of its own (daemon_loop sets
	# $DEDUPE = {} for the life of the process) -- without evicting it
	# here, ->new below would just hand back the same stale object our
	# mtime check above just decided to replace.
	%$PublicInbox::Config::DEDUPE = () if $PublicInbox::Config::DEDUPE;
	my $cfg = PublicInbox::Config->new($ENV{PI_CONFIG});
	my $new_www = PublicInbox::WWW->new($cfg);
	$new_www->preload;
	my $new_cgit = PublicInbox::Cgit->new($cfg);
	# All or nothing, and the mtime last: when any of the above dies
	# (a config git can't parse), nothing here has changed, and the
	# next request tries the file again. Taking the mtime first would
	# mark the broken file as loaded, and the old config would go on
	# serving without a word.
	($pi_cfg, $www, $cgit, $pi_cfg_mtime) =
		($cfg, $new_www, $new_cgit, $mtime);
}
load_pi_config();

# Paths and ports come from defaults.env, sourced and exported by
# entrypoint.sh before serve-web.sh starts public-inbox-netd -- one copy of
# each value, shared with the shell loops and the Python dashboard. Missing
# means the environment wasn't set up, so say so rather than guessing.
sub env {
	my ($name) = @_;
	return $ENV{$name} // die "$name is unset -- source defaults.env\n";
}

my $toplevel = env('GROKMIRROR_TOPLEVEL');
my $dashboard_host = env('DASHBOARD_HOST');
my $dashboard_port = env('DASHBOARD_PORT');
my $mcp_host = env('MCP_HOST');
my $mcp_port = env('MCP_PORT');
my $archive_upstream = env('ARCHIVE_UPSTREAM');
my $pull_stamp_path = env('PULL_STAMP_PATH');

# The archive only holds what the tracked subsystems pulled in, so a 404 or
# an empty search from it doesn't mean the mail doesn't exist. Every /lore
# response says so, and names the archive that has the rest, so that a
# client (liblore) can ask there instead. The headers are the same for
# every inbox: the inbox names here (spi-mailinglist, ...) are our own and
# lore.kernel.org has no inbox by that name, so its `all' answers for each.
#
# `updated' is when the last good pull cycle started (kgl-pull-loop.sh
# writes it), and it is what lets a client trust "nothing new" from here.
# It is read on every request, but only re-read when the file changes:
# the loop replaces it with a rename, so the inode tells a new one apart
# even within the same second. No file, or junk in it, means no
# `updated', which is always safe -- the client just asks upstream.
my ($stamp_key, $stamp) = ('');
sub pull_stamp {
	my @st = stat($pull_stamp_path);
	my $key = @st ? "$st[0]:$st[1]:$st[9]:$st[7]" : '';
	return $stamp if $key eq $stamp_key;
	$stamp_key = $key;
	$stamp = undef;
	if (@st && open(my $fh, '<', $pull_stamp_path)) {
		# All of it, not a line: anything past the one number
		# is junk too.
		my $text = do { local $/; <$fh> } // '';
		$stamp = $1 if $text =~ /\A([0-9]+)\n?\z/;
	}
	return $stamp;
}

sub archive_headers {
	my ($res) = @_;
	# ARCHIVE_UPSTREAM='' is the way back to a plain archive: no headers
	# at all, and clients treat it as a full one, as they always have.
	return $res if $archive_upstream eq '';
	my $coverage = 'partial';
	my $updated = pull_stamp();
	$coverage .= "; updated=$updated" if defined $updated;
	return Plack::Util::response_cb($res, sub {
		my ($r) = @_;
		# A copy, because WWW may hand back the same header list
		# for more than one response.
		my $h = $r->[1] = [ @{$r->[1]} ];
		Plack::Util::header_set($h, 'X-Archive-Coverage', $coverage);
		Plack::Util::header_set($h, 'X-Archive-Upstream',
					$archive_upstream);
		return;
	});
}

# HTTP::Tiny's own default is 60s, and it applies to each read as much as to
# the connect -- so a response that goes quiet for a minute is dropped as if
# the dashboard had died. It's set here rather than left implicit because
# that default was silently load bearing once, and quietly severing a
# response is a much worse failure than waiting a little longer for one.
# The dashboard's own long-running work is detached from the request that
# starts it (see app.py), so nothing it serves should take anywhere near
# this; the generous value is for the unhappy path, not the normal one.
my $proxy_timeout = 300;

# A wall-clock bound on one proxied request, for the mounts that pass it.
# $proxy_timeout cannot do this job: HTTP::Tiny applies it per read, so a
# backend that produces *something* every few seconds resets it forever and
# the request never ends. That is not hypothetical -- a pair of /mcp
# requests once pinned both workers past any timeout, and since proxy_to
# blocks the process it runs in, the two of them took the archive, cgit,
# git clones and the IMAP and NNTP listeners down with them. A worker stuck
# there is also outside the event loop, so it never acts on the flag a
# graceful SIGTERM sets and only SIGKILL clears it.
#
# Deliberately not applied to the dashboard: /api/generate streams NDJSON
# progress for as long as setup actually takes, which is tens of minutes on
# a cold start and entirely correct. Only endpoints whose responses are
# supposed to be bounded get a bound.
my $mcp_deadline = $ENV{MCP_PROXY_DEADLINE} // 120;

# git-clone repo paths mirror upstream's own layout under $toplevel (the
# same structure grok-pull mirrors into), e.g.
# /pub/scm/linux/kernel/git/torvalds/linux.git/info/refs
sub resolve_repo {
	my ($nick) = @_;
	my $dir = "$toplevel/$nick";
	return -d $dir ? PublicInbox::Git->new($dir) : undef;
}

# Served when one of the internal apps isn't accepting connections -- it's
# restarting, or the container has only just come up and it hasn't bound its
# port yet. Without this the client gets HTTP::Tiny's synthetic 599 with a
# bare "Connection refused" as the body, which reads like the whole
# container is broken rather than "not quite yet". Refreshes itself so a
# browser left open lands on the real page as soon as it's there. An MCP
# client gets HTML where it wanted JSON-RPC, which is untidy, but a 503 with
# Retry-After is the honest answer either way and the window is seconds.
sub starting_up {
	my ($what, $why) = @_;
	my $body = <<'EOM';
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>Starting up</title>
</head>
<body>
<h1>Starting up</h1>
<p>SERVICE isn't accepting connections yet. This page reloads itself
every couple of seconds.</p>
</body>
</html>
EOM
	$body =~ s/SERVICE/The $what/;
	warn "$what unreachable: $why\n";
	return [503, ['Content-Type' => 'text/html; charset=utf-8',
		'Retry-After' => '2',
		'Content-Length' => length($body)], [$body]];
}

# Refuses the standalone SSE stream an MCP client opens with GET, which
# this server has no use for and this router must never forward.
#
# Streamable HTTP lets a client open `GET /mcp' to receive messages the
# server starts on its own. Ours never does: it runs stateless with
# json_response, so every answer is the body of the POST that asked for
# it. The Python SDK opens the stream anyway -- _handle_get_request()
# doesn't consult is_json_response_enabled -- and then holds it open
# forever with nothing to say.
#
# Forwarding that is fatal, not untidy. proxy_to() blocks in
# HTTP::Tiny->request until the response ends, so a stream that never ends
# occupies the process it landed in until $proxy_timeout gives up on it.
# Workers (WEB_WORKERS) bound the damage to one of them; they do not
# repair it, because a client that reconnects its stream takes a worker
# each time and never gives one back. Under the -W0 this ran with when
# the bug was found, that process was the only one, and one attached MCP
# client stopped the dashboard, cgit, git clones and the IMAP and NNTP
# listeners together. Observed exactly that way: a curl POST worked, an
# MCP client attached, and everything stopped.
#
# 405 is what the spec asks for -- "If the server does not offer an SSE
# stream at this endpoint, the server MUST return HTTP 405" -- so a
# conforming client simply carries on posting, which is all ours needs.
sub no_sse_stream {
	my $body = '{"jsonrpc":"2.0","error":{"code":-32601,' .
		'"message":"This server has no server-initiated stream; POST instead."},' .
		'"id":null}';
	return [405, ['Content-Type' => 'application/json',
		'Allow' => 'POST, DELETE',
		'Content-Length' => length($body)], [$body]];
}

# Streaming reverse proxy to one of the internal apps. A naive
# buffer-then-forward proxy would turn /api/generate's line-by-line NDJSON
# progress (setup/app.py's send_ndjson_stream) into one blocking response,
# so this forwards each chunk to the PSGI streaming responder as it
# arrives from HTTP::Tiny's data_callback instead of waiting for EOF.
# $what names the backend for the log line and the holding page, and is the
# only thing the two mounts differ by.
sub proxy_to {
	my ($host, $port, $what, $env, $deadline) = @_;
	my $url = "http://$host:$port$env->{REQUEST_URI}";
	my $started = clock_gettime(CLOCK_MONOTONIC);

	# Host and Content-Length are HTTP::Tiny's to set from the target URL
	# and request body -- it rejects Host outright ("must not be provided
	# as header option") and a stale Content-Length from the original
	# request would be wrong here anyway. Transfer-Encoding and Trailer
	# describe how the client framed its body on its own connection,
	# which says nothing about how we frame ours: public-inbox has
	# already de-chunked the body below, and forwarding `chunked' beside
	# the Content-Length HTTP::Tiny computes would hand the backend two
	# contradictory framings of the same bytes.
	my %skip = map { $_ => 1 } qw(HOST CONTENT-LENGTH
					TRANSFER-ENCODING TRAILER);
	my %headers;
	for my $k (keys %$env) {
		next unless $k =~ /\AHTTP_(.+)\z/;
		(my $h = $1) =~ tr/_/-/;
		next if $skip{uc $h};
		$headers{$h} = $env->{$k};
	}
	$headers{'Content-Type'} = $env->{CONTENT_TYPE} if $env->{CONTENT_TYPE};

	# Read the body to EOF rather than sizing it from CONTENT_LENGTH: a
	# chunked request does not have one. PublicInbox::HTTP's
	# input_prepare() only fills CONTENT_LENGTH in the identity branch
	# (rfc7230 3.3.3 favours Transfer-Encoding over it), while the
	# chunked branch de-chunks into a tmpfile that app_dispatch()
	# rewinds before the app ever runs. Sizing off CONTENT_LENGTH
	# therefore forwarded a chunked POST with an empty body, and the
	# backend then waited for a body that was never coming -- one worker
	# blocked in HTTP::Tiny for the whole $proxy_timeout, per request.
	# The body is fully buffered by the time we are called, so reading
	# it here cannot block on a slow client.
	my %opts = (headers => \%headers);
	# The JSON-RPC method name, for the log line below. A bare
	# `POST /mcp' says nothing about what was asked for, and when a
	# request hangs that name is the only thing that identifies it.
	# Matched rather than parsed: this is one short string out of a
	# body that is being forwarded either way, and pulling in a JSON
	# decoder to read it would be the more fragile of the two.
	my $rpc;
	if ($env->{CONTENT_LENGTH} ||
			($env->{HTTP_TRANSFER_ENCODING} // '') =~ /\bchunked\b/i) {
		my ($body, $buf) = ('');
		$body .= $buf while read($env->{'psgi.input'}, $buf, 65536);
		$opts{content} = $body;
		($rpc) = $body =~ /"method"\s*:\s*"([^"]{1,64})"/;
	}

	return sub {
		my $responder = shift;
		my $writer;
		# HTTP::Tiny already de-chunks the body before handing bytes to
		# data_callback, so Transfer-Encoding: chunked from the origin
		# describes framing that no longer applies to what we write
		# here -- forwarding it verbatim (on top of PublicInbox::HTTP
		# re-chunking its own framing for a persistent connection)
		# doubly-encodes the body and the client fails to parse it.
		# Connection is similarly hop-by-hop and PublicInbox::HTTP
		# decides that for itself based on the client's own request.
		my $strip_resp_headers = sub {
			my (%h) = @_;
			delete @h{qw(Transfer-Encoding transfer-encoding
				Connection connection)};
			return %h;
		};
		my $bytes = 0;
		$opts{data_callback} = sub {
			my ($data, $resp) = @_;
			$writer //= $responder->([
				$resp->{status},
				[ $strip_resp_headers->(%{$resp->{headers}}) ],
			]);
			$bytes += length $data;
			$writer->write($data);
		};
		# The deadline has to be a signal, because there is no hook
		# inside HTTP::Tiny that a slow response reaches. data_callback
		# looks like one and is not: for a Content-Length body it fires
		# once per 32KiB buffer, and filling that buffer is itself a
		# blocking read, so a backend dribbling bytes never reaches the
		# callback at all -- measured, zero calls. $proxy_timeout does
		# not help either, being per-read: anything arriving at all
		# resets it, forever.
		#
		# SIGALRM is safe to use here in a way it usually is not. It is
		# process-wide, but this worker is doing nothing else -- it is
		# blocked in this call by construction -- and the alarm is
		# cleared on every path out. HTTP::Tiny catches the die itself
		# and turns it into a 599, which closes the socket and frees
		# the worker, so nothing escapes into the event loop.
		#
		# The signal has to be unblocked first. public-inbox reads its
		# signals from a signalfd and blocks essentially all of them to
		# do it (SigBlk is fffffffe7ffbfeff in a worker), SIGALRM
		# included, so alarm() on its own sets a timer whose signal is
		# never delivered -- it just sits pending and the deadline
		# silently does nothing. Unblock it for the duration of this
		# call and put the mask back afterwards: nothing else in this
		# daemon uses alarm(), so there is no signal here to steal.
		#
		# The handler is installed and removed by hand rather than
		# local'd to the eval, because `local' would restore it as the
		# eval unwinds -- a hair before alarm(0) runs. An alarm landing
		# in that window would find SIGALRM unblocked with no handler,
		# and the default action for it is to kill the worker.
		my $timed_out;
		my $alrm = POSIX::SigSet->new(SIGALRM);
		my $prev = POSIX::SigSet->new;
		my $prev_handler = $SIG{ALRM};
		$SIG{ALRM} = sub {
			$timed_out = 1;
			die "proxy deadline ${deadline}s exceeded\n";
		};
		if ($deadline) {
			sigprocmask(SIG_UNBLOCK, $alrm, $prev);
			alarm($deadline);
		}
		my $resp = eval {
			HTTP::Tiny->new(timeout => $proxy_timeout)
				->request($env->{REQUEST_METHOD}, $url, \%opts);
		};
		my $err = $@;
		if ($deadline) {
			alarm(0);
			sigprocmask(SIG_SETMASK, $prev);
		}
		if (defined $prev_handler) {
			$SIG{ALRM} = $prev_handler;
		} else {
			delete $SIG{ALRM};
		}
		my $elapsed = clock_gettime(CLOCK_MONOTONIC) - $started;
		# Every proxied request leaves a trace. When this went wrong
		# there was no record of what had been proxied at all -- the
		# backend's own log said 200 for a response that had merely
		# begun -- so the duration and the byte count are the point.
		# The content type separates a bounded JSON reply from an
		# event stream, which is the distinction that matters when a
		# request does not end.
		my $ctype = $resp && $resp->{headers}
			? ($resp->{headers}{'content-type'} // '') : '';
		$ctype = $ctype->[0] if ref $ctype eq 'ARRAY';
		$ctype =~ s/;.*//;
		warn sprintf("proxy: %s %s%s -> %s %s in %.1fs (%d bytes%s)\n",
			$env->{REQUEST_METHOD}, $env->{REQUEST_URI},
			defined $rpc ? " [$rpc]" : '', $what,
			$timed_out ? 'DEADLINE'
				: ($err ? 'ERROR' : ($resp->{status} // '?')),
			$elapsed, $bytes, $ctype ? ", $ctype" : '');
		if ($timed_out || $err) {
			# Once a byte has gone out the status line is spent,
			# so a cut-short body is all that can be said. Before
			# that, say plainly that the backend ran over.
			return $writer->close if $writer;
			return $responder->([504,
				['Content-Type', 'text/plain; charset=UTF-8'],
				["$what did not answer within ${deadline}s\n"]])
				if $timed_out;
			return $responder->(starting_up($what, $err));
		}
		# 599 is HTTP::Tiny's own marker for "no response at all" --
		# the connection failed, so there's nothing from the dashboard
		# to pass on and the status isn't one the origin chose.
		if (!$writer && $resp->{status} == 599) {
			$responder->(starting_up($what, $resp->{content}));
			return;
		}
		# data_callback only fires for 2xx responses (HTTP::Tiny's
		# _prepare_data_cb silently ignores it otherwise and buffers
		# into $resp->{content} instead) -- cover that case, and the
		# empty-body case (a 204, or a HEAD response), by writing
		# whatever content came back before closing.
		$writer //= $responder->([$resp->{status},
			[ $strip_resp_headers->(%{$resp->{headers}}) ]]);
		$writer->write($resp->{content}) if length $resp->{content};
		$writer->close;
	};
}

builder {
	# Honours X-Forwarded-* so the URLs public-inbox generates match the
	# site the client actually asked for rather than our own bind address.
	# Installed by the Containerfile, so a missing module here is a broken
	# image and not something to paper over at runtime.
	enable 'ReverseProxy';
	enable 'Head';

	# mount hands a bare /lore to the app with an empty PATH_INFO, and
	# PublicInbox::WWW only knows '/' as its listing -- '' falls through
	# every route into news_cgit_fallback and serves the cgit index
	# instead. Send it to /lore/ the way WWW does for a bare inbox name.
	mount '/lore' => sub {
		my $env = shift;
		if ($env->{PATH_INFO} eq '') {
			my $qs = $env->{QUERY_STRING};
			my $loc = "$env->{SCRIPT_NAME}/" .
				(length($qs // '') ? "?$qs" : '');
			return archive_headers([301, ['Location' => $loc,
				'Content-Type' => 'text/plain'],
				["Moved to $loc\n"]]);
		}
		# The headers belong on errors too: a client that sees a bare
		# 500 from here can't tell it from a full archive's 500. So a
		# die is turned into a 500 of our own rather than left to netd.
		my $res = eval {
			load_pi_config();
			$www->call($env);
		};
		unless ($res) {
			my $err = $@ || "no response\n";
			warn "lore: $env->{REQUEST_METHOD} $env->{REQUEST_URI}: $err";
			$res = [500, ['Content-Type' => 'text/plain'],
				["Internal server error\n"]];
		}
		return archive_headers($res);
	};
	# PublicInbox::Cgit->new returns undef until publicinbox.cgitrc is
	# set -- true at boot before any subsystem has been tracked yet,
	# since write_publicinbox_config (setup/app.py) only runs once the
	# maintainer submits their first selection.
	mount '/cgit' => sub {
		my $env = shift;
		load_pi_config();
		return [404, ['Content-Type' => 'text/plain'],
			['cgit not configured yet']] unless $cgit;
		# PublicInbox::Config->parse_cgitrc only recognizes
		# css/favicon/logo for its -cgit_static serve-directly
		# allowlist -- cgit's own separate `js=' directive still
		# controls the <script src> URL cgit.cgi's template emits,
		# but PublicInbox::Cgit never learns to serve that path
		# itself, so it 404s unless we serve it here the same way
		# Cgit.pm serves its other static files.
		if ($env->{PATH_INFO} eq '/cgit.js' && $cgit->{cgit_data}) {
			return PublicInbox::WwwStatic::response($env, [],
				"$cgit->{cgit_data}/cgit.js");
		}
		return $cgit->call($env);
	};
	# The MCP server, on the same terms as the dashboard: it listens only
	# on the loopback and is reached through here, so it goes wherever the
	# published port already goes -- an ssh tunnel, tailscale, or nothing
	# at all when the container is local. mount strips the prefix from
	# PATH_INFO but leaves REQUEST_URI whole, and REQUEST_URI is what gets
	# forwarded, so the server still sees the /mcp it serves on.
	mount '/mcp' => sub {
		my $env = shift;
		# GET is only ever the SSE stream; see no_sse_stream().
		return no_sse_stream() if $env->{REQUEST_METHOD} eq 'GET';
		return proxy_to($mcp_host, $mcp_port, 'MCP server', $env,
			$mcp_deadline);
	};
	mount '/' => sub {
		my $env = shift;
		if ($env->{PATH_INFO} =~
				m!\A/(.+?)/($PublicInbox::GitHTTPBackend::ANY)\z!o) {
			my ($nick, $path) = ($1, $2);
			if (my $git = resolve_repo($nick)) {
				return PublicInbox::GitHTTPBackend::serve(
					$env, $git, $path);
			}
		}
		return proxy_to($dashboard_host, $dashboard_port,
			'dashboard', $env);
	};
};
